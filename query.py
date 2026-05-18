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

SYSTEM_PROMPT = (
    "You are an academic assistant. The student has provided their Blackboard course "
    "content below, including pre-processed course summaries with assignment details "
    "and material mappings. Answer their questions accurately based on this content. "
    "When referencing assignments or documents, be specific about names, due dates, "
    "and weightings. When asked what materials can help with an assignment, use the "
    "material map and topic overlap to make specific recommendations. When asked to "
    "summarize a document, use the full document text provided. If information isn't "
    "available in the provided content, say so."
    "\n\nAt the end of your response, include a 'Sources Used' section that lists the specific Blackboard items you referenced to answer the question. For each source, include:\n- The item title\n- The course name\n- The container/folder it's in (if any)\n- The content type (PDF, Text Document, Assignment, etc.)\nFormat as a compact list. Only include items you actually used in your answer, not every item in the context."
    "\n\nAfter answering a question, suggest 2-3 brief follow-up actions the student might want to take. For example: going deeper into specific materials, creating a day-by-day study plan, summarizing a specific document, comparing assignments across courses, or identifying which topics to prioritize. Keep suggestions concise and as a short bulleted list at the end of your response."
)

PREPROCESS_SYSTEM_PROMPT = (
    "You are analyzing a university course's Blackboard content. Extract a structured "
    "summary containing:\n\n"
    "1. ASSIGNMENTS: Every graded item (essays, papers, presentations, projects, "
    "homework, quizzes, exams, midterms, finals, discussions, participation). For each include:\n"
    "   - Name\n"
    "   - Type (essay/exam/quiz/presentation/homework/discussion/other)\n"
    "   - Due date (exact date, week number, or 'not specified')\n"
    "   - Weight/percentage if mentioned\n"
    "   - Description/requirements if available\n"
    "   - Which topics or weeks it covers\n\n"
    "2. COURSE SCHEDULE: Week-by-week or class-by-class topic breakdown if available\n\n"
    "3. MATERIAL MAP: For each assignment, list which course materials (readings, "
    "PowerPoints, documents) are relevant based on topic overlap, week numbers, or "
    "explicit references\n\n"
    "Be thorough. Look inside syllabus text, assessment descriptions, and document "
    "content for assignment information that may be embedded in tables or prose. "
    "Do not miss any graded item."
)

MODEL = "claude-sonnet-4-6"
FULL_TEXT_TRIGGER_CHARS = 500  # chars before extracted_text is truncated in compact index

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


def preprocess_courses(client: anthropic.Anthropic, data: dict,
                       full_texts: dict, compact_index: dict,
                       cache_path: Path) -> dict[str, str]:
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
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=PREPROCESS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            summary = response.content[0].text
        except anthropic.RateLimitError:
            print("  Rate limited — waiting 60 seconds...", flush=True)
            time.sleep(60)
            try:
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=PREPROCESS_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_message}],
                )
                summary = response.content[0].text
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


def load_or_preprocess(client: anthropic.Anthropic, data: dict,
                       full_texts: dict, compact_index: dict,
                       json_path: str) -> dict[str, str]:
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
    return preprocess_courses(client, data, full_texts, compact_index, cache_path)


# ---------------------------------------------------------------------------
# Course matching
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def build_course_map(data: dict) -> dict[str, str]:
    """Maps course_id -> course_name."""
    return {c["course_id"]: c["course_name"] for c in data.get("courses", [])}


_CROSS_COURSE_PHRASES = [
    "all my classes", "all my courses", "all classes", "all courses",
    "across all", "every class", "every course", "each class", "each course",
    "my classes", "my courses",
]


def detect_courses(question: str, course_map: dict[str, str],
                   full_texts: dict[str, dict[str, str]] = None) -> list[str]:
    """
    Return list of course_ids that match the question, or all ids if none match
    (i.e. treat as cross-course).
    """
    q_low = question.lower()
    if any(phrase in q_low for phrase in _CROSS_COURSE_PHRASES):
        return list(course_map.keys())

    q_words = set(_words(question))
    matched = []

    for cid, cname in course_map.items():
        name_words = set(_words(cname))
        # Also generate abbreviation from capitalised words
        abbrev_words = {w[0] for w in cname.split() if w and w[0].isupper()}
        if q_words & name_words or q_words & abbrev_words:
            matched.append(cid)

    if full_texts:
        for cid in course_map:
            if cid not in matched:
                titles = list(full_texts.get(cid, {}).keys())
                if fuzzy_match_titles(question, titles):
                    matched.append(cid)

    return matched if matched else list(course_map.keys())


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
# API interaction
# ---------------------------------------------------------------------------

def build_context(course_ids: list[str], course_map: dict[str, str],
                  compact_index: dict[str, str],
                  course_summaries: dict[str, str],
                  full_texts: dict[str, dict[str, str]],
                  question: str) -> str:
    blocks = []
    for cid in course_ids:
        cname = course_map.get(cid, cid)
        parts = []

        # 1. Pre-processed summary
        summary = course_summaries.get(cid)
        if summary:
            parts.append(f"[Pre-processed Course Summary: {cname}]\n{summary}")

        # 2. Compact index
        index = compact_index.get(cid, f"(no data for {cname})")
        parts.append(f"[Course Content Index: {cname}]\n{index}")

        # 3. Full document text for any item fuzzy-matched by the question
        course_full = full_texts.get(cid, {})
        if course_full:
            matched_titles = fuzzy_match_titles(question, list(course_full.keys()))
            for title in matched_titles:
                doc_text = course_full[title]
                parts.append(f"[Full Document: {title}]\n{doc_text}")

        blocks.append("\n\n".join(parts))

    return "\n\n---\n\n".join(blocks)


def ask(client: anthropic.Anthropic, history: list[dict],
        question: str, context: str) -> str:
    """Send question + context, stream response, return full text."""
    user_content = f"[Course Content]\n{context}\n\n[Question]\n{question}"

    history.append({"role": "user", "content": user_content})

    full_response = ""
    print("\nAssistant: ", end="", flush=True)

    with client.messages.stream(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=history,
    ) as stream:
        for text in stream.text_stream:
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
    compact_index, full_texts = build_course_indexes(data)
    course_map = build_course_map(data)

    if not course_map:
        print("No courses found in file.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Pre-processing pass (cached)
    course_summaries = load_or_preprocess(
        client, data, full_texts, compact_index, json_path
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
        matched_ids = detect_courses(raw, course_map, full_texts)

        if len(matched_ids) == len(course_map):
            label = "all courses"
        else:
            label = " + ".join(short_label(course_map[cid]) for cid in matched_ids)

        print(f"  (using context: {label})")

        context = build_context(
            matched_ids, course_map, compact_index,
            course_summaries, full_texts, raw
        )

        try:
            ask(client, history, raw, context)
        except anthropic.AuthenticationError:
            print("Error: invalid API key. Check your ANTHROPIC_API_KEY.\n")
            break
        except anthropic.APIError as e:
            print(f"API error: {e}\n")


if __name__ == "__main__":
    main()
