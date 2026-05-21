"""
FastAPI web server for the Blackboard AI query engine.

Usage:
    python app.py output/content_text_20260514_084311.json
"""

import json
import os
import sys

import anthropic
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from query import (
    MODEL,
    SYSTEM_PROMPT,
    build_context,
    build_course_indexes,
    build_course_map,
    detect_courses,
    load_data,
    load_or_preprocess,
    short_label,
)

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
_client: anthropic.Anthropic | None = None
_json_path: str = ""

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
    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "courses": courses,
            "needs_attention_count": data.get("needs_attention_count", 0),
            "generated_at": data.get("generated_at", ""),
            "active_page": "calendar",
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
                "active_page": "calendar",
            },
        )

    color_map = _build_course_colors(data)
    items = [
        {**item, "course_color": color_map.get(item.get("course_id", ""), "#6b7a8d")}
        for item in data.get("needs_attention", [])
    ]
    return templates.TemplateResponse(
        request,
        "needs_attention.html",
        {
            "items": items,
            "generated_at": data.get("generated_at", ""),
            "total_count": len(items),
            "active_page": "calendar",
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
                    "confidence_score": item.get("confidence_score"),
                },
            }
        )
    return JSONResponse(events)


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/chat")
async def chat(req: ChatRequest):
    question = req.message
    api_history = req.history  # [{role, content}, ...]

    matched_ids = detect_courses(question, _course_map, _full_texts)

    if len(matched_ids) == len(_course_map):
        context_label = "all courses"
    else:
        context_label = " + ".join(
            short_label(_course_map[cid]) for cid in matched_ids
        )

    context = build_context(
        matched_ids, _course_map, _compact_index,
        _course_summaries, _full_texts, question,
    )

    user_content = f"[Course Content]\n{context}\n\n[Question]\n{question}"

    messages = list(api_history) + [{"role": "user", "content": user_content}]

    def event_stream():
        # First SSE event: context metadata
        meta = json.dumps({"context_label": context_label, "course_ids": matched_ids})
        yield f"data: {meta}\n\n"

        with _client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                payload = json.dumps({"text": text})
                yield f"data: {payload}\n\n"

        yield 'data: {"done": true}\n\n'

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def startup(json_path: str) -> None:
    global _data, _compact_index, _full_texts, _course_map, _course_summaries
    global _client, _json_path

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

    _client = anthropic.Anthropic(api_key=api_key)
    _semester_config = _load_semester_config()

    print(f"Loaded {len(_course_map)} course(s).")
    _course_summaries = load_or_preprocess(
        _client, _data, _full_texts, _compact_index, json_path, _semester_config
    )
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
