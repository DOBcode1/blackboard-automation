"""
aggregate_deadlines.py — Aggregate parsed deadlines into output/deadlines.json
for consumption by the /calendar route.

Usage:
    python aggregate_deadlines.py
    python aggregate_deadlines.py output/preprocessed_content_text_20260514_084311.json
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit_deadlines import parse_assignments_from_summary


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_most_recent_preprocessed() -> Path:
    output_dir = Path("output")
    candidates = [
        p for p in output_dir.glob("preprocessed_content_text_*.json")
        if not p.suffix == ".OLD.json" and not p.name.endswith(".OLD.json")
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        print("Error: no preprocessed_content_text_*.json files found in output/", file=sys.stderr)
        sys.exit(1)
    return candidates[0]


def _load_course_names(preprocessed_path: Path) -> dict[str, str]:
    """Return {course_id: course_name} from the paired content_text file, if it exists."""
    stem = preprocessed_path.stem  # e.g. preprocessed_content_text_20260514_084311
    paired_stem = re.sub(r"^preprocessed_", "", stem)
    paired_path = preprocessed_path.parent / f"{paired_stem}.json"
    if not paired_path.exists():
        return {}
    with open(paired_path, encoding="utf-8") as f:
        data = json.load(f)
    return {
        course["course_id"]: course["course_name"]
        for course in data.get("courses", [])
        if "course_id" in course and "course_name" in course
    }


def _slugify(text: str) -> str:
    """Convert a title to a safe slug: lowercase, spaces→hyphens, strip non-alphanumeric."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "untitled"


def _parse_confidence(raw: str) -> int:
    """
    Parse the confidence string to an int 1–5.
    Returns 3 (neutral) if missing or unparseable.
    """
    if not raw:
        return 3
    m = re.search(r"[1-5]", raw)
    return int(m.group()) if m else 3


_ISO_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _resolve_date(raw_resolved: str) -> str | None:
    """
    Return the ISO date/datetime string if it looks valid, else None.
    'UNRESOLVED', blank, or non-ISO values all map to None.
    """
    if not raw_resolved:
        return None
    s = raw_resolved.strip()
    if s.upper() == "UNRESOLVED":
        return None
    if _ISO_TIME_RE.search(s):
        # Return just the matched ISO datetime portion
        return _ISO_TIME_RE.search(s).group()
    if _ISO_DATE_RE.search(s):
        return _ISO_DATE_RE.search(s).group()
    return None


def _make_id(course_id: str, title: str, index: int) -> str:
    return f"{course_id}__{_slugify(title)}__{index}"


# ── Core aggregation ──────────────────────────────────────────────────────────

def _load_semester_window() -> tuple[datetime | None, datetime | None]:
    """
    Load semester_start/semester_end from semester_config.json "default" block.
    Returns (window_start, window_end) with ±7/+30 day buffers applied,
    or (None, None) if the file is missing or malformed.
    """
    config_path = Path("semester_config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        default = cfg["default"]
        semester_start = datetime.fromisoformat(default["semester_start"])
        semester_end = datetime.fromisoformat(default["semester_end"])
        return semester_start - timedelta(days=7), semester_end + timedelta(days=30)
    except FileNotFoundError:
        print("Warning: semester_config.json not found — skipping semester-window check", file=sys.stderr)
        return None, None
    except Exception as exc:
        print(f"Warning: could not parse semester_config.json ({exc}) — skipping semester-window check", file=sys.stderr)
        return None, None


def aggregate(preprocessed_path: Path) -> dict:
    with open(preprocessed_path, encoding="utf-8") as f:
        raw_data = json.load(f)

    if not isinstance(raw_data, dict):
        print("Error: expected preprocessed cache format {course_id: summary_text}", file=sys.stderr)
        sys.exit(1)

    course_names = _load_course_names(preprocessed_path)
    window_start, window_end = _load_semester_window()

    # Build per-course title → index counter for stable IDs
    title_counters: dict[str, defaultdict] = {}

    resolved_items: list[dict] = []
    needs_attention_items: list[dict] = []
    seen_course_ids: list[str] = []

    for course_id, summary_text in raw_data.items():
        if not isinstance(summary_text, str):
            continue

        course_name = course_names.get(course_id, course_id)
        seen_course_ids.append(course_id)

        if "pre-processing failed" in summary_text.lower():
            continue

        assignments = parse_assignments_from_summary(course_id, course_name, summary_text)
        if course_id not in title_counters:
            title_counters[course_id] = defaultdict(int)

        for assignment in assignments:
            title = assignment.get("title", "").strip()
            if not title:
                continue

            slug = _slugify(title)
            idx = title_counters[course_id][slug]
            title_counters[course_id][slug] += 1

            confidence = _parse_confidence(assignment.get("confidence", ""))
            due_resolved = _resolve_date(assignment.get("due_date_resolved", ""))

            # Determine assignment type: use parsed field, fall back to 'unknown'
            atype = (assignment.get("assignment_type") or "").strip().lower() or "unknown"

            source_link = (assignment.get("source_link") or "").strip() or None

            entry = {
                "id": _make_id(course_id, title, idx),
                "course_id": course_id,
                "course_name": course_name,
                "title": title,
                "type": atype,
                "due_date_raw": assignment.get("due_date_raw", "") or "",
                "due_date_resolved": due_resolved,
                "confidence_score": confidence,
                "source_link": source_link,
                "source_item_id": None,  # not available from preprocessed text
            }

            # Semester-window check
            flag_reason: str | None = None
            if due_resolved is not None and window_start is not None and window_end is not None:
                # Parse just the date portion for comparison (handles both date and datetime strings)
                due_date_str = due_resolved[:10]
                try:
                    due_dt = datetime.fromisoformat(due_date_str)
                    if due_dt < window_start or due_dt > window_end:
                        flag_reason = "outside_semester_window"
                except ValueError:
                    pass  # unparseable date — leave flag_reason as None

            entry["flag_reason"] = flag_reason

            # Bucketing: outside window → needs_attention regardless of confidence
            # Otherwise: resolved = non-null date AND confidence >= 3
            if flag_reason == "outside_semester_window":
                needs_attention_items.append(entry)
            elif due_resolved is not None and confidence >= 3:
                resolved_items.append(entry)
            else:
                # Assign specific flag_reason for needs_attention items
                if due_resolved is None:
                    entry["flag_reason"] = "unresolved_date"
                else:
                    entry["flag_reason"] = "low_confidence"
                needs_attention_items.append(entry)

    # Unique ordered course list
    unique_course_ids = list(dict.fromkeys(seen_course_ids))
    courses = [
        {"course_id": cid, "course_name": course_names.get(cid, cid)}
        for cid in unique_course_ids
    ]

    total = len(resolved_items) + len(needs_attention_items)
    flagged_window_count = sum(
        1 for e in needs_attention_items if e.get("flag_reason") == "outside_semester_window"
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_file": preprocessed_path.name,
        "course_count": len(unique_course_ids),
        "deadline_count": total,
        "resolved_count": len(resolved_items),
        "needs_attention_count": len(needs_attention_items),
        "flagged_outside_window_count": flagged_window_count,
        "courses": courses,
        "resolved": resolved_items,
        "needs_attention": needs_attention_items,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate deadlines from a preprocessed Blackboard cache into deadlines.json"
    )
    parser.add_argument(
        "preprocessed_file",
        nargs="?",
        help="Path to preprocessed_content_text_*.json (auto-detected if omitted)",
    )
    args = parser.parse_args()

    if args.preprocessed_file:
        path = Path(args.preprocessed_file)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
    else:
        path = _find_most_recent_preprocessed()
        print(f"Auto-detected: {path}")

    result = aggregate(path)

    out_path = Path("output") / "deadlines.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Source: {result['source_file']}")
    print(f"Courses: {result['course_count']}")
    print(f"Total deadlines: {result['deadline_count']}")
    print(f"Resolved (calendar-ready): {result['resolved_count']}")
    print(f"Needs attention: {result['needs_attention_count']}")
    print(f"Flagged outside semester window: {result['flagged_outside_window_count']}")
    print(f"Written to: {out_path}")


if __name__ == "__main__":
    main()
