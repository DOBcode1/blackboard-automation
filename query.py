"""
Phase 4: AI Query Engine — interactive Q&A over Blackboard reader output.

Usage:
    python query.py output/content_text_20260320_114928.json
"""

import json
import os
import sys
import re

import anthropic

SYSTEM_PROMPT = (
    "You are an academic assistant. The student has provided their Blackboard course "
    "content below. Answer their questions accurately based on this content. When "
    "referencing assignments or documents, be specific about names and due dates. "
    "If information isn't available in the provided content, say so."
)

MODEL = "claude-sonnet-4-6"
FULL_TEXT_TRIGGER_CHARS = 500  # chars before extracted_text is truncated in compact index


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
# Course matching
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def build_course_map(data: dict) -> dict[str, str]:
    """Maps course_id -> course_name."""
    return {c["course_id"]: c["course_name"] for c in data.get("courses", [])}


def detect_courses(question: str, course_map: dict[str, str]) -> list[str]:
    """
    Return list of course_ids that match the question, or all ids if none match
    (i.e. treat as cross-course).
    """
    q_words = set(_words(question))
    matched = []

    for cid, cname in course_map.items():
        name_words = set(_words(cname))
        # Also generate abbreviation from capitalised words
        abbrev_words = {w[0] for w in cname.split() if w and w[0].isupper()}
        if q_words & name_words or q_words & abbrev_words:
            matched.append(cid)

    return matched if matched else list(course_map.keys())


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
                  compact_index: dict[str, str]) -> str:
    blocks = []
    for cid in course_ids:
        blocks.append(compact_index.get(cid, f"(no data for {course_map.get(cid, cid)})"))
    return "\n\n---\n\n".join(blocks)


def ask(client: anthropic.Anthropic, history: list[dict],
        question: str, context: str) -> str:
    """Send question + context, stream response, return full text."""
    # Inject context into the user message
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
    history: list[dict] = []

    print(f"\nLoaded {len(course_map)} course(s) from {data.get('term', 'unknown term')}.")
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
        matched_ids = detect_courses(raw, course_map)

        if len(matched_ids) == len(course_map):
            label = "all courses"
        else:
            label = " + ".join(short_label(course_map[cid]) for cid in matched_ids)

        print(f"  (using context: {label})")

        context = build_context(matched_ids, course_map, compact_index)

        try:
            ask(client, history, raw, context)
        except anthropic.AuthenticationError:
            print("Error: invalid API key. Check your ANTHROPIC_API_KEY.\n")
            break
        except anthropic.APIError as e:
            print(f"API error: {e}\n")


if __name__ == "__main__":
    main()
