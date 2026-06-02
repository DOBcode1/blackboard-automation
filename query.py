"""
Phase 4: AI Query Engine — interactive Q&A over Blackboard reader output.

Usage:
    python query.py output/content_text_20260320_114928.json
"""

import json
import os
import sys
import re
import time
from pathlib import Path

import anthropic
from llm_adapter import call_fast, call_main
from retrieval import search as retrieval_search
import documents_helper

SYSTEM_PROMPT = (
    "You are an academic assistant. The student has provided their Blackboard course "
    "content below, including pre-processed course summaries with assignment details "
    "and material mappings. Answer their questions accurately based on this content. "
    "When referencing assignments or documents, be specific about names, due dates, "
    "and weightings. When asked what materials can help with an assignment, use the "
    "material map and topic overlap to make specific recommendations. When asked to "
    "summarize a document, use the full document text provided. If information isn't "
    "available in the provided content, say so."
    "\n\nWhen generating study guides, practice tests, or other study materials, clearly state at the beginning of your response whether the content is based entirely on course materials from Blackboard, or whether you are also drawing on your general knowledge to supplement. If you use general knowledge to fill gaps, flag those specific sections so the student knows what came from their course vs. general knowledge."
    "\n\nAt the end of your response, include a 'Sources Used' section that lists the specific Blackboard items you referenced to answer the question. For each source, include:\n- The item title\n- The course name\n- The container/folder it's in (if any)\n- The content type (PDF, Text Document, Assignment, etc.)\nFormat as a compact list. Only include items you actually used in your answer, not every item in the context."
    "\n\nAfter answering a question, suggest 2-3 brief follow-up actions the student might want to take. For example: going deeper into specific materials, creating a day-by-day study plan, summarizing a specific document, comparing assignments across courses, or identifying which topics to prioritize. Keep suggestions concise and as a short bulleted list at the end of your response."
    "\n\nThe deadline information below has been corrected based on user edits where applicable. Treat dates and titles as authoritative."
    "\n\nIf the context begins with a [USER-CONFIRMED OVERRIDES] block, those entries are authoritative. Trust them over anything that follows, including raw document text."
)

def load_semester_config() -> dict:
    """Load semester_config.json from project root. Returns empty dict on missing file."""
    config_path = Path("semester_config.json")
    if not config_path.exists():
        print(
            "Warning: semester_config.json not found — calendar resolution will be degraded."
        )
        return {}
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def get_semester_anchor(config: dict, course_id: str) -> dict:
    """Return the semester anchor for a course, falling back to default."""
    courses = config.get("courses", {})
    if course_id in courses:
        return courses[course_id]
    return config.get("default", {})


def build_preprocess_prompt(semester_start: str, semester_end: str, term_name: str) -> str:
    anchor_section = (
        f"\n\nSEMESTER ANCHOR (use for date resolution):\n"
        f"  term_name:      {term_name}\n"
        f"  semester_start: {semester_start}\n"
        f"  semester_end:   {semester_end}\n"
    ) if semester_start else ""

    return (
        "You are analyzing a university course's Blackboard content. Extract a structured "
        "summary containing:\n\n"
        "1. ASSIGNMENTS: Every graded item (essays, papers, presentations, projects, "
        "homework, quizzes, exams, midterms, finals, discussions, participation). "
        "For each, output a section with this EXACT format:\n\n"
        "### Assignment\n"
        "- **Name:** <name>\n"
        "- **Type:** essay/exam/quiz/presentation/homework/discussion/other\n"
        "- **Due Date:** <verbatim text from syllabus, e.g. 'Week 7', 'October 15', 'TBD'>\n"
        "- **Due Date Resolved:** <ISO date YYYY-MM-DD or datetime YYYY-MM-DDTHH:MM, or UNRESOLVED>\n"
        "- **Weight:** <percentage or 'not specified'>\n"
        "- **Confidence:** <integer 1–5>\n\n"
        "Due Date Resolved rules:\n"
        "  - Week N = semester_start + (N-1) weeks. If no day specified, use the Friday of that week.\n"
        "  - Explicit date in text → parse and reformat to ISO.\n"
        "  - Time specified (e.g. '10:00 PM') → include as YYYY-MM-DDTHH:MM.\n"
        "  - Unresolvable ('ongoing', 'TBD', 'see instructor') → output exactly: UNRESOLVED\n"
        "  - Two conflicting dates → use most authoritative source "
        "(assignment page > syllabus table > syllabus body) and note conflict in parenthetical.\n\n"
        "Confidence scale:\n"
        "  5 = explicitly stated date on Blackboard assignment page\n"
        "  4 = explicit date in syllabus or course schedule\n"
        "  3 = inferred from 'Week N' with semester anchor\n"
        "  2 = inferred from context with some ambiguity\n"
        "  1 = best guess; user should verify\n"
        "  Output as integer only, e.g. '**Confidence:** 4'\n\n"
        "2. COURSE SCHEDULE: Week-by-week or class-by-class topic breakdown if available\n\n"
        "3. MATERIAL MAP: For each assignment, list which course materials (readings, "
        "PowerPoints, documents) are relevant based on topic overlap, week numbers, or "
        "explicit references\n\n"
        "Be thorough. Look inside syllabus text, assessment descriptions, and document "
        "content for assignment information that may be embedded in tables or prose. "
        "Do not miss any graded item."
        + anchor_section
    )

MODEL = "claude-sonnet-4-6"
FULL_TEXT_TRIGGER_CHARS = 500  # chars before extracted_text is truncated in compact index
RETRIEVAL_TOP_K_PER_COURSE = 10

_ASSESSMENT_TYPES = {
    "assignment", "assessment", "test", "exam", "quiz", "discussion",
    "survey", "turnitin", "scorm",
}

_KEY_TITLE_KEYWORDS = {
    "syllabus", "schedule", "timeline", "course outline", "course info",
    "course description", "course overview", "grading", "requirements", "policies",
}

_KEY_TEXT_PHRASES = {
    "grade", "grading", "assessment", "final exam", "midterm",
    "percentage", "weight", "due date", "submission",
}

_LONG_DOC_TYPES = {"text document", "pdf"}
_LONG_DOC_THRESHOLD = 3000  # chars

print(
    "Note: delete output/preprocessed_*.json to force re-preprocessing "
    "if you change key item detection."
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_data(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_course_indexes(data: dict) -> tuple[dict, dict]:
    """
    Returns:
        compact_index  — {course_id: str}   compact summary string
        full_texts     — {course_id: {item_title: full_extracted_text}}
    """
    compact_index: dict[str, str] = {}
    full_texts: dict[str, dict[str, str]] = {}

    for course in data.get("courses", []):
        cid = course["course_id"]
        cname = course["course_name"]
        items = course.get("content_objects", [])

        lines = [f"Course: {cname}\n"]
        full_texts[cid] = {}

        for item in items:
            title = item.get("title") or "(untitled)"
            ctype = item.get("content_type") or ""
            container = item.get("container_name") or ""
            due = item.get("due_date") or item.get("due_date_raw") or ""
            desc = item.get("description") or ""
            subtext = item.get("subtext") or ""
            raw_text = item.get("extracted_text") or ""

            # Store full text keyed by title for on-demand retrieval
            if raw_text:
                full_texts[cid][title] = raw_text

            # Build compact line
            snippet = raw_text[:FULL_TEXT_TRIGGER_CHARS]
            if len(raw_text) > FULL_TEXT_TRIGGER_CHARS:
                snippet += "…"

            parts = [f"- [{ctype}] {title}"]
            if container:
                parts.append(f"  Container: {container}")
            if due:
                parts.append(f"  Due: {due}")
            if desc:
                parts.append(f"  Description: {desc}")
            if subtext:
                parts.append(f"  Note: {subtext}")
            if snippet:
                parts.append(f"  Content: {snippet}")

            lines.append("\n".join(parts))

        compact_index[cid] = "\n\n".join(lines)

    return compact_index, full_texts


# ---------------------------------------------------------------------------
# Pre-processing
# ---------------------------------------------------------------------------

def _is_key_item(item: dict) -> bool:
    """Return True if this item should be included in pre-processing context."""
    title = (item.get("title") or "").lower()
    ctype = (item.get("content_type") or "").lower()
    raw_text = (item.get("extracted_text") or "").lower()

    if any(kw in title for kw in _KEY_TITLE_KEYWORDS):
        return True
    if any(kw in ctype for kw in _ASSESSMENT_TYPES):
        return True
    if ctype in _LONG_DOC_TYPES and len(raw_text) > _LONG_DOC_THRESHOLD:
        return True
    if any(phrase in raw_text for phrase in _KEY_TEXT_PHRASES):
        return True
    return False


def preprocess_courses(data: dict,
                       full_texts: dict, compact_index: dict,
                       cache_path: Path,
                       semester_config: dict) -> dict[str, str]:
    """
    For each course, send key item texts + compact index to Claude and store
    a structured summary. Caches results to cache_path.
    Returns course_summaries: {course_id: summary_text}
    """
    course_summaries: dict[str, str] = {}

    for course in data.get("courses", []):
        cid = course["course_id"]
        cname = course["course_name"]
        items = course.get("content_objects", [])

        print(f"Pre-processing {cname}...", flush=True)

        anchor = get_semester_anchor(semester_config, cid)
        prompt = build_preprocess_prompt(
            anchor.get("semester_start", ""),
            anchor.get("semester_end", ""),
            anchor.get("term_name", ""),
        )

        # Gather full text for key items
        key_blocks = []
        for item in items:
            if not _is_key_item(item):
                continue
            title = item.get("title") or "(untitled)"
            raw_text = item.get("extracted_text") or ""
            due = item.get("due_date") or item.get("due_date_raw") or ""
            desc = item.get("description") or ""
            ctype = item.get("content_type") or ""

            block_lines = [f"=== [{ctype}] {title} ==="]
            if due:
                block_lines.append(f"Due: {due}")
            if desc:
                block_lines.append(f"Description: {desc}")
            if raw_text:
                block_lines.append(f"Full text:\n{raw_text}")
            key_blocks.append("\n".join(block_lines))

        key_texts_str = "\n\n".join(key_blocks) if key_blocks else "(no key items found)"
        index_str = compact_index.get(cid, "")

        user_message = (
            f"Course: {cname}\n\n"
            f"[Key Item Full Texts]\n{key_texts_str}\n\n"
            f"[Full Course Content Index]\n{index_str}"
        )

        fallback = "(pre-processing failed for this course — using compact index only)"
        try:
            response = call_main(
                messages=[{"role": "user", "content": user_message}],
                system=prompt,
                max_tokens=4096,
            )
            summary = response.text
        except anthropic.RateLimitError:
            print("  Rate limited — waiting 60 seconds...", flush=True)
            time.sleep(60)
            try:
                response = call_main(
                    messages=[{"role": "user", "content": user_message}],
                    system=prompt,
                    max_tokens=4096,
                )
                summary = response.text
            except anthropic.RateLimitError:
                print(f"  Warning: rate limit retry failed for {cname}. Using fallback.", flush=True)
                summary = fallback
        except anthropic.APIError as e:
            print(f"  Warning: API error for {cname}: {e}. Using fallback.", flush=True)
            summary = fallback

        course_summaries[cid] = summary
        print(f"  Done. ({len(summary)} chars)", flush=True)
        time.sleep(15)

    # Save cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(course_summaries, f, ensure_ascii=False, indent=2)
    print(f"Pre-processing cache saved to {cache_path}\n")

    return course_summaries


def load_or_preprocess(data: dict,
                       full_texts: dict, compact_index: dict,
                       json_path: str,
                       semester_config: dict) -> dict[str, str]:
    """Load pre-processed summaries from cache if fresh, otherwise re-run."""
    stem = Path(json_path).stem
    cache_path = Path("output") / f"preprocessed_{stem}.json"

    if cache_path.exists():
        input_mtime = os.path.getmtime(json_path)
        cache_mtime = os.path.getmtime(cache_path)
        if cache_mtime >= input_mtime:
            print(f"Loading pre-processed summaries from cache ({cache_path})…")
            with open(cache_path, encoding="utf-8") as f:
                summaries = json.load(f)
            print(f"  Loaded {len(summaries)} course summary/summaries.\n")
            return summaries

    print("Running pre-processing (this may take a moment)…\n")
    return preprocess_courses(data, full_texts, compact_index, cache_path,
                              semester_config)


# ---------------------------------------------------------------------------
# Course matching
# ---------------------------------------------------------------------------

def build_course_map(data: dict) -> dict[str, str]:
    """Maps course_id -> course_name."""
    return {c["course_id"]: c["course_name"] for c in data.get("courses", [])}


def route_question_to_courses(
    question: str,
    course_map: dict[str, str],
) -> list[str]:
    """
    Use Haiku to determine which course_ids are relevant to the question.
    Falls back to all course_ids on any failure.
    """
    all_ids = list(course_map.keys())
    course_list = "\n".join(f"{cid}: {cname}" for cid, cname in course_map.items())
    system = (
        "You are a course router. Given a student's question and a list of courses, "
        "return a JSON array of course_ids that are relevant to the question.\n"
        "Rules:\n"
        "- If the question names a specific course or topic, return only that course's id.\n"
        "- If the question is general or cross-course (e.g. 'what's due', 'my schedule', "
        "'what's coming up', 'all my classes', 'my semester', 'what do I have'), return ALL course_ids.\n"
        "- If ambiguous, prefer returning all course_ids over fewer.\n"
        "- NEVER return an empty array.\n"
        "- Output ONLY a valid JSON array of id strings. No prose, no markdown, no code fences."
    )
    user_msg = f"Question: {question}\n\nAvailable courses:\n{course_list}"

    try:
        response = call_fast(
            messages=[{"role": "user", "content": user_msg}],
            system=system,
            max_tokens=256,
        )
        raw_json = response.text.strip()
        parsed = json.loads(raw_json)
        valid = [cid for cid in parsed if cid in course_map]
        if not valid:
            print("[router] Parsed list empty after validation — falling back to all courses", file=sys.stderr)
            return all_ids
        print(f"[router] Routed to: {valid}", file=sys.stderr)
        return valid
    except anthropic.RateLimitError as e:
        print(f"[router] Haiku failed (RateLimitError) — falling back to all courses", file=sys.stderr)
        return all_ids
    except anthropic.APIError as e:
        print(f"[router] Haiku failed (APIError) — falling back to all courses", file=sys.stderr)
        return all_ids
    except json.JSONDecodeError as e:
        print(f"[router] Haiku failed (JSONDecodeError) — falling back to all courses", file=sys.stderr)
        return all_ids
    except Exception as e:
        print(f"[router] Haiku failed ({type(e).__name__}) — falling back to all courses", file=sys.stderr)
        return all_ids


# ---------------------------------------------------------------------------
# Fuzzy document matching
# ---------------------------------------------------------------------------

_STOP_WORDS = {"the", "a", "an", "of", "for", "and", "or", "in", "on", "to",
               "is", "it", "at", "by", "be", "as", "my", "me"}


def _normalize_chapter(text: str) -> str:
    """Normalize chapter references: 'chapter 7', 'ch 7', 'ch7' → 'ch7'."""
    text = re.sub(r'\bchapter\s*(\d+)\b', r'ch\1', text)
    text = re.sub(r'\bch\s+(\d+)\b', r'ch\1', text)
    return text


def fuzzy_match_titles(question: str, titles: list[str]) -> list[str]:
    """Return titles from the list that fuzzy-match the question."""
    q_low = question.lower()
    q_norm = _normalize_chapter(q_low)
    q_tokens = set(re.findall(r'[a-z0-9]+', q_norm)) - _STOP_WORDS
    matched = []

    for title in titles:
        t_low = title.lower()
        t_norm = _normalize_chapter(t_low)

        # Direct substring match (either direction)
        if t_norm in q_norm or q_norm in t_norm:
            matched.append(title)
            continue

        # Token overlap: ≥50% of title tokens must appear in question (min 2)
        t_tokens = set(re.findall(r'[a-z0-9]+', t_norm)) - _STOP_WORDS
        if t_tokens and q_tokens:
            overlap = q_tokens & t_tokens
            if len(overlap) >= max(2, len(t_tokens) // 2):
                matched.append(title)

    return matched


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def short_label(course_name: str) -> str:
    """Strip the 'Spring 2026 ...' prefix and course code suffix for display."""
    name = re.sub(r"^Spring \d{4}\s+", "", course_name)
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    return name.strip()


def print_courses(course_map: dict[str, str]) -> None:
    print("\nAvailable courses:")
    for i, (cid, cname) in enumerate(course_map.items(), 1):
        print(f"  [{i}] {short_label(cname)}")
    print()


# ---------------------------------------------------------------------------
# Override → markdown rewriting
# ---------------------------------------------------------------------------

def _find_assignment_block(markdown: str, title: str) -> tuple[int, int] | None:
    """Return (start, end) offsets of the ### Assignment block whose Name matches title."""
    search = title.lower().strip()
    for m in re.finditer(r'### Assignment\s*\n', markdown, re.IGNORECASE):
        block_start = m.start()
        rest = markdown[m.end():]
        next_h = re.search(r'\n(?=###)', rest)
        if next_h:
            block_end = m.end() + next_h.start() + 1
        else:
            block_end = len(markdown)
        block = markdown[block_start:block_end]
        nm = re.search(r'\*\*Name:\*\*\s*(.+)', block)
        if nm and nm.group(1).strip().lower() == search:
            return block_start, block_end
    return None


def _update_block_field(block: str, field: str, value: str) -> str:
    """Replace a single field line within an ### Assignment block."""
    if field == "title":
        return re.sub(r'(\*\*Name:\*\*\s*).+', lambda m: m.group(1) + value, block)
    if field == "due_date_resolved":
        return re.sub(r'(\*\*Due Date Resolved:\*\*\s*).+', lambda m: m.group(1) + value, block)
    if field == "type":
        return re.sub(r'(\*\*Type:\*\*\s*).+', lambda m: m.group(1) + value, block)
    if field == "notes":
        if re.search(r'\*\*User notes:\*\*', block):
            return re.sub(r'(\*\*User notes:\*\*\s*).+', lambda m: m.group(1) + value, block)
        return block.rstrip('\n') + f'\n- **User notes:** {value}\n'
    return block


def _build_manual_assignment_block(item: dict) -> str:
    """Build a new ### Assignment block from a manual_add override item."""
    lines = [
        "### Assignment",
        f"- **Name:** {item.get('title', 'Untitled')}",
        f"- **Type:** {item.get('type', 'manual')}",
        "- **Due Date:** (user-added)",
        f"- **Due Date Resolved:** {item.get('due_date_resolved') or 'UNRESOLVED'}",
        "- **Weight:** not specified",
        "- **Confidence:** 5",
    ]
    notes = item.get("notes")
    if notes:
        lines.append(f"- **User notes:** {notes}")
    return "\n".join(lines) + "\n"


def apply_overrides_to_markdown(course_id: str, markdown: str, deadlines_data: dict) -> str:
    """
    Rewrite ### Assignment blocks in a preprocessed course summary to reflect
    user overrides from deadlines_data (the merged view in deadlines.json).

    - dismissed items: block removed entirely
    - user_edited items: named fields updated in-place
    - manual_add items not yet in markdown: new block appended
    """
    override_items = []
    for section in ("resolved", "needs_attention", "dismissed"):
        for item in deadlines_data.get(section, []):
            if item.get("course_id") != course_id:
                continue
            if item.get("user_edited") or item.get("dismissed") or item.get("manual_add"):
                override_items.append(item)

    if not override_items:
        return markdown

    for item in override_items:
        is_dismissed = item.get("dismissed", False)
        is_user_edited = item.get("user_edited", False)
        is_manual_add = item.get("manual_add", False)
        title = item.get("title", "")

        # For user-edited items, the current title may differ from the markdown;
        # use ai_original.title to locate the original block.
        search_title = title
        if is_user_edited:
            ai_orig = item.get("ai_original", {})
            if ai_orig.get("title"):
                search_title = ai_orig["title"]

        if is_manual_add and not is_dismissed:
            span = _find_assignment_block(markdown, search_title)
            if span is None and search_title != title:
                span = _find_assignment_block(markdown, title)
            if span is None:
                new_block = _build_manual_assignment_block(item)
                sec = re.search(r'(## ASSIGNMENTS.*?)(\n## |\Z)', markdown, re.DOTALL | re.IGNORECASE)
                if sec:
                    markdown = markdown[:sec.end(1)] + "\n" + new_block + markdown[sec.end(1):]
                else:
                    markdown = markdown.rstrip('\n') + "\n\n" + new_block
            continue

        span = _find_assignment_block(markdown, search_title)
        if span is None and search_title != title:
            span = _find_assignment_block(markdown, title)

        if span is None:
            print(
                f"[chat-sync] Warning: could not find assignment '{search_title}' "
                f"(course {course_id}) — skipping override",
                file=sys.stderr,
            )
            continue

        start, end = span

        if is_dismissed:
            markdown = markdown[:start] + markdown[end:]
        elif is_user_edited:
            block = markdown[start:end]
            for field in item.get("user_edited_fields", []):
                val = item.get(field)
                if val is not None:
                    block = _update_block_field(block, field, str(val))
            markdown = markdown[:start] + block + markdown[end:]

    return markdown


# ---------------------------------------------------------------------------
# Overrides block
# ---------------------------------------------------------------------------

def build_overrides_block(deadlines_data: dict, course_map: dict) -> str:
    """
    Build a [USER-CONFIRMED OVERRIDES] block from all user_edited, dismissed,
    or manual_add items across all courses. Returns an empty string if none exist.
    """
    if not deadlines_data:
        return ""

    # Collect overrides grouped by course_id
    by_course: dict[str, list[dict]] = {}
    for section in ("resolved", "needs_attention", "dismissed"):
        for item in deadlines_data.get(section, []):
            if not (item.get("user_edited") or item.get("dismissed") or item.get("manual_add")):
                continue
            cid = item.get("course_id", "")
            by_course.setdefault(cid, []).append(item)

    if not by_course:
        return ""

    lines = [
        "[USER-CONFIRMED OVERRIDES — AUTHORITATIVE]",
        "The user has manually corrected the following deadlines. These supersede any",
        "conflicting information in summaries, document text, or the item index below.",
        "",
    ]

    for cid, items in by_course.items():
        cname = course_map.get(cid, cid)
        lines.append(f"Course: {cname}")
        for item in items:
            title = item.get("title", "(untitled)")
            lines.append(f'- "{title}"')

            if item.get("dismissed"):
                lines.append("    Status: User dismissed (not a real deadline; ignore)")
            elif item.get("manual_add"):
                due = item.get("due_date_resolved") or item.get("due_date") or "UNRESOLVED"
                itype = item.get("type", "")
                lines.append(f"    Due: {due}")
                if itype:
                    lines.append(f"    Type: {itype}")
                lines.append("    Status: User-added manually")
            elif item.get("user_edited"):
                due = item.get("due_date_resolved") or item.get("due_date") or "UNRESOLVED"
                itype = item.get("type", "")
                lines.append(f"    Due: {due}")
                if itype:
                    lines.append(f"    Type: {itype}")
                ai_orig = item.get("ai_original", {})
                orig_due = ai_orig.get("due_date_resolved") or ai_orig.get("due_date") or ""
                orig_note = f" (original AI extraction was {orig_due})" if orig_due else ""
                lines.append(f"    Status: User-edited{orig_note}")

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# API interaction
# ---------------------------------------------------------------------------

def build_context(course_ids: list[str], course_map: dict[str, str],
                  course_summaries: dict[str, str],
                  question: str,
                  deadlines_data: dict | None = None) -> str:
    overrides_block = build_overrides_block(deadlines_data or {}, course_map)

    # Build document id -> title lookup once per call
    doc_id_to_title = {doc["id"]: doc["title"] for doc in documents_helper.list_documents()}

    blocks = []
    for cid in course_ids:
        cname = course_map.get(cid, cid)
        parts = []

        # 1. Pre-processed summary (with user overrides applied)
        summary = course_summaries.get(cid)
        if summary:
            if deadlines_data is not None:
                summary = apply_overrides_to_markdown(cid, summary, deadlines_data)
            parts.append(f"[Pre-processed Course Summary: {cname}]\n{summary}")

        # 2. Relevant retrieved content (replaces compact index + fuzzy full-doc parts)
        results = retrieval_search(question, course_id=cid, top_k=RETRIEVAL_TOP_K_PER_COURSE)
        for result in results:
            if result.get("metadata", {}).get("content_type") == "course_summary":
                continue
            doc_title = doc_id_to_title.get(result["document_id"], "document")
            parts.append(f"[Relevant excerpt from {doc_title}]\n{result['text']}")

        blocks.append("\n\n".join(parts))

    body = "\n\n---\n\n".join(blocks)
    if overrides_block:
        return overrides_block + "\n---\n\n" + body
    return body


def ask(history: list[dict],
        question: str, context: str) -> str:
    """Send question + context, stream response, return full text."""
    user_content = f"[Course Content]\n{context}\n\n[Question]\n{question}"

    history.append({"role": "user", "content": user_content})

    full_response = ""
    print("\nAssistant: ", end="", flush=True)

    for text in call_main(messages=history, system=SYSTEM_PROMPT, max_tokens=4096, stream=True):
        print(text, end="", flush=True)
        full_response += text

    print("\n")
    history.append({"role": "assistant", "content": full_response})
    return full_response


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python query.py <content_text_file.json>")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable is not set.")
        print("Set it with:  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    json_path = sys.argv[1]
    if not os.path.exists(json_path):
        print(f"Error: file not found: {json_path}")
        sys.exit(1)

    print(f"Loading {json_path}…")
    data = load_data(json_path)
    semester_config = load_semester_config()
    compact_index, full_texts = build_course_indexes(data)
    course_map = build_course_map(data)

    deadlines_data: dict | None = None
    deadlines_path = Path("output/deadlines.json")
    if deadlines_path.exists():
        try:
            with open(deadlines_path, encoding="utf-8") as f:
                deadlines_data = json.load(f)
        except Exception as exc:
            print(f"Warning: could not load deadlines.json ({exc}) — overrides will not apply to chat.", file=sys.stderr)
    else:
        print("Note: output/deadlines.json not found — overrides will not apply to chat.", file=sys.stderr)

    if not course_map:
        print("No courses found in file.")
        sys.exit(1)

    # Pre-processing pass (cached)
    course_summaries = load_or_preprocess(
        data, full_texts, compact_index, json_path, semester_config
    )

    history: list[dict] = []

    print(f"Loaded {len(course_map)} course(s) from {data.get('term', 'unknown term')}.")
    print_courses(course_map)
    print("Type your question, 'switch' to reset context, or 'quit' to exit.\n")

    while True:
        try:
            raw = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not raw:
            continue

        low = raw.lower()

        if low in ("quit", "exit"):
            print("Goodbye.")
            break

        if low == "switch":
            history.clear()
            print_courses(course_map)
            print("Conversation history cleared.\n")
            continue

        # Detect which course(s) apply
        matched_ids = route_question_to_courses(raw, course_map)

        if len(matched_ids) == len(course_map):
            label = "all courses"
        else:
            label = " + ".join(short_label(course_map[cid]) for cid in matched_ids)

        print(f"  (using context: {label})")

        context = build_context(
            matched_ids, course_map, course_summaries, raw,
            deadlines_data,
        )

        try:
            ask(history, raw, context)
        except anthropic.AuthenticationError:
            print("Error: invalid API key. Check your ANTHROPIC_API_KEY.\n")
            break
        except anthropic.APIError as e:
            print(f"API error: {e}\n")


if __name__ == "__main__":
    main()
