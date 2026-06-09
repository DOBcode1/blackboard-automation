"""
FastAPI web server for the Blackboard AI query engine.

Usage:
    python app.py output/content_text_20260514_084311.json
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import anthropic
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from aggregate_deadlines import write_aggregated
from overrides_helper import (
    load_overrides,
    save_overrides,
    get_override,
    set_override,
    set_dismissed,
    set_manual_add,
    clear_override,
)
from chat_history_helper import (
    load_history,
    save_history,
    create_thread,
    get_thread,
    list_threads,
    append_message,
    soft_delete_thread,
    restore_thread,
    rename_thread,
    purge_old_deleted,
    purge_thread,
    clear_all_threads,
    get_settings,
    set_memory_enabled,
    auto_title_from_message,
)

from documents_helper import get_document, update_document
from extractors import extract_text_from_file, extract_text_via_vision
from ingestion import ingest_text

from query import (
    MODEL,
    SYSTEM_PROMPT,
    build_context,
    build_course_indexes,
    build_course_map,
    route_question_to_courses,
    load_data,
    load_or_preprocess,
    short_label,
)
from llm_adapter import call_main

# ---------------------------------------------------------------------------
# Calendar — color palette & template setup
# ---------------------------------------------------------------------------

# Fixed palette of 6 accessible colors, one per course.
# Courses are sorted alphabetically by course_id and assigned by index:
#   index 0 → _6205935_1 (Philosophical Ethics)       → blue
#   index 1 → _6206084_1 (Operations & Supply Chain)  → green
#   index 2 → _6207787_1 (International Internship)   → red
#   index 3 → _6209533_1 (Fintech)                    → orange
#   index 4 → _6210142_1 (Ethics in Business)         → purple
#   index 5 → _6210387_1 (Legal Framework)            → cyan
_COURSE_COLOR_PALETTE = [
    "#2563eb",  # blue
    "#16a34a",  # green
    "#dc2626",  # red
    "#ea580c",  # orange
    "#9333ea",  # purple
    "#0891b2",  # cyan
]

_DEADLINES_PATH = "output/deadlines.json"
_SEMESTER_CONFIG_PATH = "semester_config.json"

templates = Jinja2Templates(directory="templates")


def _load_deadlines() -> dict | None:
    """Return parsed deadlines.json, or None if missing."""
    if not os.path.exists(_DEADLINES_PATH):
        return None
    with open(_DEADLINES_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_semester_config() -> dict:
    """Return parsed semester_config.json, or {} if missing/malformed."""
    if not os.path.exists(_SEMESTER_CONFIG_PATH):
        print("Warning: semester_config.json not found — calendar resolution will be degraded.")
        return {}
    try:
        with open(_SEMESTER_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Warning: could not load semester_config.json ({e}) — calendar resolution will be degraded.")
        return {}


def _build_course_colors(deadlines_data: dict) -> dict[str, str]:
    """Return {course_id: color} by sorting course_ids alphabetically."""
    course_ids = sorted(c["course_id"] for c in deadlines_data.get("courses", []))
    return {
        cid: _COURSE_COLOR_PALETTE[i % len(_COURSE_COLOR_PALETTE)]
        for i, cid in enumerate(course_ids)
    }


# ---------------------------------------------------------------------------
# Global state (populated at startup)
# ---------------------------------------------------------------------------

_data: dict = {}
_compact_index: dict[str, str] = {}
_full_texts: dict[str, dict[str, str]] = {}
_course_map: dict[str, str] = {}
_course_summaries: dict[str, str] = {}
_json_path: str = ""
_deadlines_data: dict | None = None

# In-memory incognito threads — never written to disk
_incognito_threads: dict[str, dict] = {}

# In-memory upload job status — keyed by job_id
_upload_jobs: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Blackboard Assistant")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"active_page": "chat"},
    )


@app.get("/api/courses")
async def get_courses():
    return {
        "courses": [
            {"id": cid, "name": cname, "label": short_label(cname)}
            for cid, cname in _course_map.items()
        ]
    }


@app.get("/calendar")
async def calendar_page(request: Request):
    data = _load_deadlines()
    if data is None:
        return templates.TemplateResponse(
            request,
            "calendar.html",
            {
                "courses": [],
                "needs_attention_count": 0,
                "generated_at": None,
                "active_page": "calendar",
            },
        )

    color_map = _build_course_colors(data)
    courses = [
        {
            "course_id": c["course_id"],
            "course_name": c["course_name"],
            "color": color_map.get(c["course_id"], "#6b7a8d"),
        }
        for c in data.get("courses", [])
    ]
    sem = _load_semester_config()
    default = sem.get("default", {})
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "courses": courses,
            "needs_attention_count": data.get("needs_attention_count", 0),
            "generated_at": data.get("generated_at", ""),
            "active_page": "calendar",
            "semester_start": default.get("semester_start", ""),
            "semester_end": default.get("semester_end", ""),
        },
    )


@app.get("/calendar/needs-attention")
async def needs_attention_page(request: Request):
    data = _load_deadlines()
    if data is None:
        return templates.TemplateResponse(
            request,
            "needs_attention.html",
            {
                "items": None,
                "generated_at": None,
                "total_count": 0,
                "dismissed_items": [],
                "active_page": "calendar",
            },
        )

    color_map = _build_course_colors(data)
    items = [
        {**item, "course_color": color_map.get(item.get("course_id", ""), "#6b7a8d")}
        for item in data.get("needs_attention", [])
    ]
    dismissed = [
        {**item, "course_color": color_map.get(item.get("course_id", ""), "#6b7a8d")}
        for item in data.get("dismissed", [])
    ]
    sem = _load_semester_config()
    default = sem.get("default", {})
    resolved_dates = [
        item["due_date_resolved"]
        for item in data.get("resolved", [])
        if item.get("due_date_resolved")
    ]
    return templates.TemplateResponse(
        request,
        "needs_attention.html",
        {
            "items": items,
            "generated_at": data.get("generated_at", ""),
            "total_count": len(items),
            "dismissed_items": dismissed,
            "active_page": "calendar",
            "semester_start": default.get("semester_start", ""),
            "semester_end": default.get("semester_end", ""),
            "resolved_dates": resolved_dates,
        },
    )


@app.get("/api/deadlines")
async def api_deadlines():
    data = _load_deadlines()
    if data is None:
        return JSONResponse([])

    color_map = _build_course_colors(data)
    events = []
    for item in data.get("resolved", []):
        start = item.get("due_date_resolved", "")
        if not start:
            continue
        all_day = "T" not in start  # date-only strings have no 'T'
        color = color_map.get(item.get("course_id", ""), "#6b7a8d")
        events.append(
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "start": start,
                "allDay": all_day,
                "backgroundColor": color,
                "borderColor": color,
                "extendedProps": {
                    "course_id": item.get("course_id", ""),
                    "course_name": item.get("course_name", ""),
                    "type": item.get("type", ""),
                    "due_date_raw": item.get("due_date_raw", ""),
                    "due_date_resolved": item.get("due_date_resolved", ""),
                    "confidence_score": item.get("confidence_score"),
                    "notes": item.get("notes", ""),
                    "user_edited": item.get("user_edited", False),
                    "user_edited_fields": item.get("user_edited_fields", []),
                    "manual_add": item.get("manual_add", False),
                },
            }
        )
    return JSONResponse(events)


# ---------------------------------------------------------------------------
# Overrides helpers
# ---------------------------------------------------------------------------

def _deadline_id_exists(deadline_id: str) -> bool:
    """Return True if deadline_id appears in deadlines.json or in existing overrides."""
    data = _load_deadlines()
    if data:
        for section in ("resolved", "needs_attention"):
            for item in data.get(section, []):
                if item.get("id") == deadline_id:
                    return True
    overrides = load_overrides()
    return deadline_id in overrides.get("overrides", {})


def _run_aggregator() -> str | None:
    """Re-run the aggregator; return a warning string on failure, None on success."""
    try:
        write_aggregated()
        return None
    except Exception as exc:
        msg = f"aggregation failed: {exc}"
        print(f"[overrides] {msg}")
        return msg


# ---------------------------------------------------------------------------
# Override endpoints
# ---------------------------------------------------------------------------

@app.put("/api/overrides/{deadline_id}")
async def put_override(deadline_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)

    edits = body.get("edits")
    dismissed = body.get("dismissed")

    if edits is None and dismissed is None:
        return JSONResponse(
            {"error": "at least one of 'edits' or 'dismissed' must be present"},
            status_code=400,
        )

    if not _deadline_id_exists(deadline_id):
        return JSONResponse({"error": f"deadline_id not found: {deadline_id}"}, status_code=404)

    overrides = load_overrides()

    if edits:
        for field, value in edits.items():
            overrides = set_override(overrides, deadline_id, field, value)

    if dismissed is not None:
        overrides = set_dismissed(overrides, deadline_id, dismissed)

    try:
        save_overrides(overrides)
    except Exception as exc:
        return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

    warning = _run_aggregator()

    logged_fields = list(edits.keys()) if edits else []
    if dismissed is not None:
        logged_fields.append("dismissed")
    print(f"[overrides] PUT {deadline_id} fields: {logged_fields}")

    result: dict = {"deadline_id": deadline_id, "override": get_override(overrides, deadline_id)}
    if warning:
        result["warning"] = warning
    return JSONResponse(result)


@app.post("/api/overrides/manual")
async def post_override_manual(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)

    deadline_id = body.get("deadline_id")
    edits = body.get("edits") or {}

    if not deadline_id:
        return JSONResponse({"error": "'deadline_id' is required"}, status_code=400)
    if not edits.get("title"):
        return JSONResponse({"error": "'edits.title' is required"}, status_code=400)
    if not edits.get("due_date_resolved"):
        return JSONResponse({"error": "'edits.due_date_resolved' is required"}, status_code=400)

    overrides = load_overrides()

    if get_override(overrides, deadline_id) is not None:
        return JSONResponse(
            {"error": f"override already exists for deadline_id: {deadline_id}"},
            status_code=409,
        )

    for field, value in edits.items():
        overrides = set_override(overrides, deadline_id, field, value)
    overrides = set_manual_add(overrides, deadline_id, True)

    try:
        save_overrides(overrides)
    except Exception as exc:
        return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

    warning = _run_aggregator()

    print(f"[overrides] POST manual {deadline_id} fields: {list(edits.keys())}")

    result: dict = {"deadline_id": deadline_id, "override": get_override(overrides, deadline_id)}
    if warning:
        result["warning"] = warning
    return JSONResponse(result)


@app.delete("/api/overrides/{deadline_id}/field/{field_name}")
async def delete_override_field(deadline_id: str, field_name: str):
    overrides = load_overrides()

    entry = get_override(overrides, deadline_id)
    if entry is None:
        return JSONResponse({"error": f"no override found for: {deadline_id}"}, status_code=404)
    if field_name not in entry.get("edits", {}):
        return JSONResponse(
            {"error": f"field '{field_name}' not found on override for: {deadline_id}"},
            status_code=404,
        )

    overrides = clear_override(overrides, deadline_id, field_name)

    try:
        save_overrides(overrides)
    except Exception as exc:
        return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

    warning = _run_aggregator()

    print(f"[overrides] DELETE {deadline_id}/field/{field_name}")

    remaining = get_override(overrides, deadline_id)
    result: dict = (
        {"deadline_id": deadline_id, "deleted": True}
        if remaining is None
        else {"deadline_id": deadline_id, "override": remaining}
    )
    if warning:
        result["warning"] = warning
    return JSONResponse(result)


@app.delete("/api/overrides/{deadline_id}")
async def delete_override(deadline_id: str):
    overrides = load_overrides()

    if get_override(overrides, deadline_id) is None:
        return JSONResponse({"error": f"no override found for: {deadline_id}"}, status_code=404)

    overrides = clear_override(overrides, deadline_id)

    try:
        save_overrides(overrides)
    except Exception as exc:
        return JSONResponse({"error": f"save failed: {exc}"}, status_code=500)

    warning = _run_aggregator()

    print(f"[overrides] DELETE {deadline_id}")

    result: dict = {"deadline_id": deadline_id, "cleared": True}
    if warning:
        result["warning"] = warning
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Thread endpoints
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def api_get_settings():
    return JSONResponse(get_settings())


@app.put("/api/settings")
async def api_put_settings(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if "memory_enabled" in body:
        set_memory_enabled(bool(body["memory_enabled"]))
    return JSONResponse(get_settings())


@app.get("/api/threads")
async def api_list_threads():
    return JSONResponse(list_threads(include_deleted=False))


@app.get("/api/threads/deleted")
async def api_list_deleted_threads():
    data = load_history()
    deleted = [t for t in data["threads"] if t.get("deleted_at") is not None]
    return JSONResponse(sorted(deleted, key=lambda t: t.get("deleted_at", ""), reverse=True))


@app.post("/api/threads")
async def api_create_thread(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    incognito = bool(body.get("incognito", False))
    title = body.get("title") or None

    if incognito:
        tid = f"thread_{int(time.time() * 1000)}"
        now = datetime.now(timezone.utc).isoformat()
        thread = {
            "id": tid,
            "title": title or "Incognito chat",
            "created_at": now,
            "updated_at": now,
            "incognito": True,
            "deleted_at": None,
            "messages": [],
            "documents": [],
        }
        _incognito_threads[tid] = thread
        return JSONResponse(thread)

    tid = create_thread(title=title, incognito=False)
    return JSONResponse(get_thread(tid))


@app.get("/api/threads/{thread_id}")
async def api_get_thread(thread_id: str):
    if thread_id in _incognito_threads:
        return JSONResponse(_incognito_threads[thread_id])
    thread = get_thread(thread_id)
    if thread is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(thread)


@app.put("/api/threads/{thread_id}")
async def api_rename_thread(thread_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    title = body.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "'title' is required"}, status_code=400)
    if thread_id in _incognito_threads:
        _incognito_threads[thread_id]["title"] = title[:50]
        return JSONResponse({"thread_id": thread_id, "title": title[:50]})
    rename_thread(thread_id, title)
    return JSONResponse({"thread_id": thread_id, "title": title[:50]})


@app.delete("/api/threads/{thread_id}")
async def api_delete_thread(thread_id: str, permanent: bool = False):
    if thread_id in _incognito_threads:
        del _incognito_threads[thread_id]
        return JSONResponse({"thread_id": thread_id, "deleted": True})
    if permanent:
        ok = purge_thread(thread_id)
        if not ok:
            return JSONResponse({"error": "thread not found"}, status_code=404)
        return JSONResponse({"thread_id": thread_id, "permanently_deleted": True})
    soft_delete_thread(thread_id)
    return JSONResponse({"thread_id": thread_id, "deleted": True})


@app.post("/api/threads/{thread_id}/restore")
async def api_restore_thread(thread_id: str):
    restore_thread(thread_id)
    thread = get_thread(thread_id)
    if thread is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(thread)


@app.delete("/api/threads")
async def api_clear_all_threads():
    count = clear_all_threads()
    return JSONResponse({"cleared": count})


# ---------------------------------------------------------------------------
# Incognito turn helper
# ---------------------------------------------------------------------------

def _save_incognito_turn(
    thread_id: str,
    user_msg: str,
    assistant_msg: str,
    attachments: list[dict] | None = None,
) -> None:
    """Update in-memory incognito thread with a completed turn."""
    thread = _incognito_threads.get(thread_id)
    if thread is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    if not thread["messages"] and thread["title"] == "Incognito chat":
        thread["title"] = auto_title_from_message(user_msg)
    thread["messages"].append({
        "role": "user",
        "content": user_msg,
        "timestamp": now,
        "attachments": attachments if attachments is not None else [],
    })
    thread["messages"].append({"role": "assistant", "content": assistant_msg, "timestamp": now})
    thread["updated_at"] = now


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []
    thread_id: str | None = None
    attachments: list[str] = []


@app.post("/api/chat")
async def chat(req: ChatRequest):
    question = req.message
    api_history = req.history
    req_thread_id = req.thread_id

    is_attachment_summary = not question.strip() and bool(req.attachments)
    question_for_model = (
        "Summarize the attached document(s), share the key insights, and tell me which of my courses this most likely relates to and how it connects."
        if is_attachment_summary else question
    )

    if is_attachment_summary:
        matched_ids = list(_course_map.keys())
    else:
        matched_ids = route_question_to_courses(question, _course_map)

    if len(matched_ids) == len(_course_map):
        context_label = "all courses"
    else:
        context_label = " + ".join(
            short_label(_course_map[cid]) for cid in matched_ids
        )

    # Resolve thread — backward compat: auto-create if none provided
    thread_id = req_thread_id
    is_incognito = thread_id in _incognito_threads if thread_id else False
    if thread_id is None:
        thread_id = create_thread()
        is_incognito = False

    # --- Collect all attachment doc_ids across the conversation ---
    # Gather from prior user messages stored in this thread, then union with
    # the current request's attachments, so follow-up questions still see them.
    prior_doc_ids: list[str] = []
    if is_incognito:
        thread_msgs = _incognito_threads[thread_id].get("messages", [])
    else:
        stored_thread = get_thread(thread_id)
        thread_msgs = stored_thread["messages"] if stored_thread else []
    is_first_message = not thread_msgs
    for msg in thread_msgs:
        if msg.get("role") == "user":
            for att in msg.get("attachments") or []:
                did = att.get("doc_id")
                if did and did not in prior_doc_ids:
                    prior_doc_ids.append(did)
    combined_doc_ids: list[str] = list(prior_doc_ids)
    for did in req.attachments:
        if did not in combined_doc_ids:
            combined_doc_ids.append(did)

    # Build attachment metadata list for storing on the user message record.
    attachment_meta: list[dict] = []
    for did in req.attachments:
        doc = get_document(did)
        name = doc.get("original_filename") or did if doc else did
        attachment_meta.append({"doc_id": did, "name": name})

    # Reload deadlines fresh on each request so edits applied while the app
    # is running are reflected immediately without a restart.
    current_deadlines = _load_deadlines() or _deadlines_data

    context = build_context(
        matched_ids, _course_map, _course_summaries, question_for_model,
        current_deadlines,
        attachments=combined_doc_ids,
    )

    user_content = f"[Course Content]\n{context}\n\n[Question]\n{question_for_model}"
    messages = list(api_history) + [{"role": "user", "content": user_content}]

    def event_stream():
        full_parts: list[str] = []

        # First SSE event: context metadata + thread_id
        meta = json.dumps({
            "context_label": context_label,
            "course_ids": matched_ids,
            "thread_id": thread_id,
        })
        yield f"data: {meta}\n\n"

        try:
            for text in call_main(messages=messages, system=SYSTEM_PROMPT, max_tokens=4096, stream=True):
                full_parts.append(text)
                payload = json.dumps({"text": text})
                yield f"data: {payload}\n\n"

            # Persist after successful stream
            full_response = "".join(full_parts)
            if is_incognito:
                _save_incognito_turn(thread_id, question, full_response, attachments=attachment_meta)
                if is_attachment_summary and attachment_meta and is_first_message:
                    _incognito_threads[thread_id]["title"] = auto_title_from_message(attachment_meta[0]["name"])
            else:
                append_message(thread_id, "user", question, attachments=attachment_meta)
                if is_attachment_summary and attachment_meta and is_first_message:
                    rename_thread(thread_id, auto_title_from_message(attachment_meta[0]["name"]))
                append_message(thread_id, "assistant", full_response)

        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        yield 'data: {"done": true}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Document upload
# ---------------------------------------------------------------------------

_ALLOWED_EXTENSIONS = {".txt", ".md", ".docx", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_UPLOAD_USER_ID = "local_dev"

_EXT_TO_MIME = {
    ".txt":  "text/plain",
    ".md":   "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _process_upload(job_id: str, data: bytes, safe_name: str, mime_type: str, thread_id: str | None) -> None:
    """Background worker: extract, ingest, and save an uploaded file."""
    job = _upload_jobs[job_id]

    # --- Extract text ---
    try:
        text, method, confidence = extract_text_from_file(data, safe_name, mime_type)
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"Extraction error: {exc}"
        return

    if method == "needs_vision":
        try:
            text, method, confidence = extract_text_via_vision(data, safe_name, mime_type)
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"Vision extraction error: {exc}"
            return

    if not text:
        job["status"] = "failed"
        job["error"] = "No readable text could be extracted."
        return

    # --- Ingest ---
    title = os.path.splitext(safe_name)[0]
    doc_id = ingest_text(
        text,
        title=title,
        source_type="user_upload",
        extraction_method=method,
        extraction_confidence=confidence,
        content_type="other",
        original_filename=safe_name,
        mime_type=mime_type,
        file_size_bytes=len(data),
        thread_id=thread_id,
    )
    if doc_id is None:
        job["status"] = "failed"
        job["error"] = "No readable text could be extracted."
        return

    # --- Save original file bytes ---
    upload_dir = os.path.join("output", "uploads", _UPLOAD_USER_ID, doc_id)
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(upload_dir, safe_name)
    with open(file_path, "wb") as fh:
        fh.write(data)
    update_document(doc_id, original_file_path=file_path)

    print(f"[upload] ingested {safe_name!r} → doc_id={doc_id} method={method}")
    job["status"] = "ready"
    job["doc_id"] = doc_id


@app.post("/api/documents/upload")
async def upload_document(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    thread_id: str | None = Form(default=None),
):
    # --- 1. Sanitize filename ---
    raw_name = file.filename or ""
    safe_name = os.path.basename(raw_name)
    if not safe_name or ".." in safe_name or "/" in safe_name or "\\" in safe_name:
        return JSONResponse({"error": "Invalid filename."}, status_code=400)

    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return JSONResponse(
            {"error": f"Unsupported file type {ext!r}. Allowed: {', '.join(sorted(_ALLOWED_EXTENSIONS))}."},
            status_code=400,
        )

    # --- 2. Read bytes and enforce size limit ---
    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"error": f"File exceeds the 20 MB limit ({len(data) / 1_048_576:.1f} MB received)."},
            status_code=400,
        )

    mime_type = file.content_type or _EXT_TO_MIME.get(ext, "application/octet-stream")

    # --- 3. Schedule background processing ---
    job_id = "job_" + uuid.uuid4().hex
    _upload_jobs[job_id] = {
        "id": job_id,
        "status": "processing",
        "filename": safe_name,
        "thread_id": thread_id,
        "doc_id": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    background_tasks.add_task(_process_upload, job_id, data, safe_name, mime_type, thread_id)
    return JSONResponse({"job_id": job_id, "status": "processing"}, status_code=202)


@app.get("/api/uploads/{job_id}")
async def get_upload_job(job_id: str):
    job = _upload_jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse(job)


_UPLOADS_ROOT = os.path.join("output", "uploads")


@app.get("/api/documents/{doc_id}/file")
async def get_document_file(doc_id: str):
    doc = get_document(doc_id)
    if doc is None:
        return JSONResponse({"error": "document not found"}, status_code=404)

    file_path = doc.get("original_file_path")
    if not file_path:
        return JSONResponse({"error": "no file path stored for this document"}, status_code=404)

    # Safety: only serve files inside the uploads directory
    abs_file = os.path.realpath(file_path)
    abs_root = os.path.realpath(_UPLOADS_ROOT)
    if not abs_file.startswith(abs_root + os.sep) and abs_file != abs_root:
        return JSONResponse({"error": "file path is outside uploads directory"}, status_code=404)

    if not os.path.isfile(abs_file):
        return JSONResponse({"error": "file not found on disk"}, status_code=404)

    return FileResponse(
        abs_file,
        media_type=doc.get("mime_type") or "application/octet-stream",
        filename=doc.get("original_filename") or os.path.basename(abs_file),
    )


@app.get("/api/documents/{doc_id}")
async def get_document_meta(doc_id: str):
    doc = get_document(doc_id)
    if doc is None:
        return JSONResponse({"error": "document not found"}, status_code=404)
    return JSONResponse({
        "id": doc_id,
        "original_filename": doc.get("original_filename"),
        "mime_type": doc.get("mime_type"),
        "extracted_text": doc.get("extracted_text"),
    })


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def startup(json_path: str) -> None:
    global _data, _compact_index, _full_texts, _course_map, _course_summaries
    global _json_path, _deadlines_data

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    if not os.path.exists(json_path):
        print(f"Error: file not found: {json_path}")
        sys.exit(1)

    _json_path = json_path
    print(f"Loading {json_path}…")
    _data = load_data(json_path)
    _compact_index, _full_texts = build_course_indexes(_data)
    _course_map = build_course_map(_data)

    if not _course_map:
        print("No courses found in file.")
        sys.exit(1)

    _semester_config = _load_semester_config()

    print(f"Loaded {len(_course_map)} course(s).")
    _course_summaries = load_or_preprocess(
        _data, _full_texts, _compact_index, json_path, _semester_config
    )

    if os.path.exists(_DEADLINES_PATH):
        try:
            with open(_DEADLINES_PATH, encoding="utf-8") as f:
                _deadlines_data = json.load(f)
            print(f"Loaded deadlines from {_DEADLINES_PATH}.")
        except Exception as exc:
            print(f"Warning: could not load {_DEADLINES_PATH} ({exc}) — overrides will not apply to chat.")
    else:
        print(f"Note: {_DEADLINES_PATH} not found — overrides will not apply to chat.")

    purged = purge_old_deleted(days=30)
    if purged:
        print(f"Purged {purged} chat thread(s) deleted >30 days ago.")

    print("Ready.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python app.py <content_text_file.json>")
        sys.exit(1)

    startup(sys.argv[1])
    uvicorn.run(app, host="127.0.0.1", port=8000)
