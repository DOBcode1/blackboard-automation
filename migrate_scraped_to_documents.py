"""
migrate_scraped_to_documents.py — one-shot migration of scraped content into the document store.

Usage:
    python migrate_scraped_to_documents.py output/content_text_<timestamp>.json

For each course, for each content_object with non-empty extracted_text, calls
ingestion.ingest_text and stores the item URL in user_provided_metadata.

Idempotency keys:
  - Scraped items:    source_type + course_id + source_url (from metadata).
                      Falls back to title when the item has no URL.
  - Course summaries: source_type + course_id + content_type == "course_summary"
                      (one summary per course; no URL exists for these).

If a corresponding preprocessed_content_text_<timestamp>.json exists alongside
the input file, also ingests each course's summary string as its own document.
"""

import json
import sys
from pathlib import Path

import documents_helper
import ingestion

SOURCE_TYPE = "blackboard_scraped"

# Conservative mapping from Blackboard content_type strings to document store content_type.
_CONTENT_TYPE_MAP: dict[str, str] = {
    "Assignment":      "assignment",
    "Test":            "assignment",
    "Discussion":      "discussion",
    "PDF":             "document",
    "Text Document":   "document",
    "Presentation":    "document",
    "Link":            "other",
    "Video":           "other",
    "Photo":           "other",
    "Learning Module": "other",
    "Open Folder":     "other",
}


def _map_content_type(raw: str) -> str:
    return _CONTENT_TYPE_MAP.get(raw, "other")


def _build_course_index(course_id: str) -> tuple[set[str], set[str], bool]:
    """Load existing documents for this course and return dedup lookup structures.

    Returns:
        known_urls:    set of source_url values already ingested (from metadata).
        known_titles:  set of titles already ingested (fallback for URL-less items).
        has_summary:   True if a course_summary document already exists.
    """
    existing = documents_helper.list_documents(course_id=course_id, source_type=SOURCE_TYPE)
    known_urls: set[str] = set()
    known_titles: set[str] = set()
    has_summary = False
    for doc in existing:
        url = (doc.get("user_provided_metadata") or {}).get("source_url", "")
        if url:
            known_urls.add(url)
        else:
            known_titles.add(doc["title"])
        if doc.get("content_type") == "course_summary":
            has_summary = True
    return known_urls, known_titles, has_summary


def main(input_path: Path) -> None:
    # Locate optional preprocessed summaries file.
    preprocessed_path = input_path.parent / input_path.name.replace(
        "content_text_", "preprocessed_content_text_"
    )
    summaries: dict[str, str] = {}
    if preprocessed_path.exists():
        try:
            raw = json.loads(preprocessed_path.read_text(encoding="utf-8"))
            summaries = {k: v for k, v in raw.items() if isinstance(v, str) and v.strip()}
            print(f"Loaded {len(summaries)} course summaries from {preprocessed_path.name}")
        except Exception as exc:
            print(f"WARNING: could not load {preprocessed_path}: {exc} -- skipping summaries")
    else:
        print(f"No preprocessed summaries file found at {preprocessed_path.name} -- skipping summaries")

    data = json.loads(input_path.read_text(encoding="utf-8"))
    courses = data.get("courses", [])

    total_created = 0
    total_skipped_exists = 0
    total_skipped_empty = 0
    total_chunks_embedded = 0

    for course in courses:
        course_id = course.get("course_id", "")
        course_name = course.get("course_name", "")
        content_objects = course.get("content_objects", [])

        # Build dedup index once per course (one list_documents call).
        known_urls, known_titles, has_summary = _build_course_index(course_id)

        for obj in content_objects:
            title = (obj.get("title") or "").strip()
            extracted_text = obj.get("extracted_text") or ""
            source_url = (obj.get("url") or "").strip()

            if not extracted_text.strip():
                total_skipped_empty += 1
                continue

            if not title:
                total_skipped_empty += 1
                continue

            # Idempotency check: prefer URL, fall back to title.
            if source_url:
                if source_url in known_urls:
                    total_skipped_exists += 1
                    continue
            else:
                if title in known_titles:
                    total_skipped_exists += 1
                    continue

            user_meta = {"source_url": source_url} if source_url else {}

            doc_id = ingestion.ingest_text(
                extracted_text,
                title=title,
                source_type=SOURCE_TYPE,
                extraction_method="native_text",
                content_type=_map_content_type(obj.get("content_type", "")),
                course_id=course_id,
                user_provided_metadata=user_meta,
            )

            if doc_id is not None:
                doc = documents_helper.get_document(doc_id)
                chunk_count = doc["chunk_count"] if doc else 0
                total_created += 1
                total_chunks_embedded += chunk_count
                # Update in-memory index to catch within-run duplicates.
                if source_url:
                    known_urls.add(source_url)
                else:
                    known_titles.add(title)
            else:
                total_skipped_empty += 1

        # Ingest course summary if available.
        summary_text = summaries.get(course_id, "").strip()
        if summary_text:
            if has_summary:
                total_skipped_exists += 1
            else:
                doc_id = ingestion.ingest_text(
                    summary_text,
                    title=f"{course_name} -- Course Summary",
                    source_type=SOURCE_TYPE,
                    extraction_method="ai_generated",
                    content_type="course_summary",
                    course_id=course_id,
                )
                if doc_id is not None:
                    doc = documents_helper.get_document(doc_id)
                    chunk_count = doc["chunk_count"] if doc else 0
                    total_created += 1
                    total_chunks_embedded += chunk_count
                    has_summary = True
                else:
                    total_skipped_empty += 1

    print()
    print("Migration complete.")
    print(f"  Documents created:          {total_created}")
    print(f"  Documents skipped (exists): {total_skipped_exists}")
    print(f"  Chunks embedded:            {total_chunks_embedded}")
    print(f"  Items skipped (empty text): {total_skipped_empty}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} output/content_text_<timestamp>.json")
        sys.exit(1)

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"ERROR: file not found: {path}")
        sys.exit(1)

    main(path)
