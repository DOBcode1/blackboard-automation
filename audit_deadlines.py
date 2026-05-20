"""
audit_deadlines.py — Quality audit for AI-extracted deadlines in preprocessed cache files.

Analyzes the most recent preprocessed_*.json in output/ and prints a structured
report assessing deadline extraction quality before building a calendar feature.

Usage:
    python audit_deadlines.py
    python audit_deadlines.py output/preprocessed_content_text_20260514_084311.json
"""

import json
import os
import re
import sys
import random
from collections import Counter
from datetime import datetime
from pathlib import Path

# ── Fuzzy matching: prefer rapidfuzz, fall back to difflib ───────────────────
try:
    from rapidfuzz import fuzz as _rfuzz
    def _similarity(a: str, b: str) -> float:
        return _rfuzz.token_sort_ratio(a, b)
    FUZZY_LIB = "rapidfuzz"
except ImportError:
    import difflib
    def _similarity(a: str, b: str) -> float:
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100
    FUZZY_LIB = "difflib"

DUPLICATE_THRESHOLD = 85  # 0–100 scale


# ── Date classification ───────────────────────────────────────────────────────

# Priority order: iso_datetime > iso_date > date_string > relative > vague > missing

_VAGUE_RE = re.compile(
    r'\b(tbd|tba|not specified|end of (semester|term|year|course)'
    r'|before (spring|fall|winter|summer) break|to be announced|to be determined'
    r'|flexible|ongoing|see (syllabus|instructor|course page)'
    r'|check (blackboard|canvas|course page)|varies|rolling|as assigned)\b',
    re.I
)

_RELATIVE_RE = re.compile(
    r'\bweek\s*\d+\b'
    r'|\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b'
    r'|\b(before|by|during)\s+class\b'
    r'|\b(class|session|lecture|module|unit|lab)\s*\d+\b',
    re.I
)

_MONTH_PAT = (
    r'january|february|march|april|may|june|july|august|september|october|november|december'
    r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec'
)

_DATE_STRING_RE = re.compile(
    rf'(?:{_MONTH_PAT})\s+\d{{1,2}}(?:,?\s*\d{{4}})?'
    r'|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b'
    r'|\b\d{1,2}-\d{1,2}(?:-\d{2,4})?\b',
    re.I
)

_ISO_DATE_RE = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
_ISO_TIME_RE = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}')
_TIME_RE = re.compile(r'\b\d{1,2}:\d{2}\s*(?:am|pm|AM|PM)?\b')


def classify_date(raw: str) -> str:
    """Classify a date string into one of six audit categories."""
    if not raw or raw.strip().lower() in ("", "none", "null", "n/a"):
        return "missing"

    s = raw.strip()

    if _ISO_TIME_RE.search(s):
        return "iso_datetime"
    if _ISO_DATE_RE.search(s):
        return "iso_date"

    # Natural language date string — check before relative/vague
    # A date_string must contain a recognisable calendar date (month name or MM/DD)
    if _DATE_STRING_RE.search(s):
        return "date_string"

    if _RELATIVE_RE.search(s):
        return "relative"

    if _VAGUE_RE.search(s):
        return "vague"

    # Fallback: if it contains any digit that isn't part of a date pattern,
    # treat as relative; otherwise vague
    if re.search(r'\d', s):
        return "relative"

    return "vague"


def has_time(raw: str, date_cat: str) -> bool:
    """Return True if the entry has a time component or explicit all-day info."""
    if date_cat == "iso_datetime":
        return True
    return bool(raw and _TIME_RE.search(raw))


# ── Parsing preprocessed summaries ───────────────────────────────────────────

def _extract_field(block: str, *field_names: str) -> str:
    """
    Extract the value of a bold markdown field like **Name:** value ...
    Captures everything up to the next bold-field marker or end of block.
    """
    for fname in field_names:
        pattern = re.compile(
            rf'\*\*{re.escape(fname)}[:\s]*\*\*\s*'   # **Field:**
            rf'(.*?)'                                   # value (lazy)
            rf'(?=\n\s*[-*]\s*\*\*|\n#{1,4}\s|\Z)',    # stop at next field / heading / end
            re.I | re.S
        )
        m = pattern.search(block)
        if m:
            value = m.group(1).strip()
            value = re.sub(r'\*\*|\*|`', '', value)                   # strip markdown
            value = re.sub(r'\s*\n\s*[-*]\s+', '; ', value)           # flatten bullet lists
            value = re.sub(r'\s*\n\s*', ' ', value).strip()           # collapse newlines
            if value and value.lower() not in ('none', 'n/a', 'not mentioned', 'not available', ''):
                return value
    return ""


def _parse_assignments_section(section_text: str) -> list[str]:
    """
    Split an ASSIGNMENTS section into individual assignment blocks.
    Handles both ### headings and --- dividers.
    """
    # Split on lines that start a new ### heading
    parts = re.split(r'\n(?=#{1,4}\s)', section_text)
    blocks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Strip leading --- dividers
        part = re.sub(r'^---+\s*\n', '', part).strip()
        if part:
            blocks.append(part)
    return blocks


_ASSIGNMENT_HEADING_RE = re.compile(
    r'^#{1,4}\s+'
    r'(?:Assignment|Exam|Quiz|Midterm|Final|Homework|Discussion|Paper|Project'
    r'|Presentation|Lab|Participation|Assessment|Test|Essay|Survey|Portfolio'
    r'|Writing|Reading Assignment|Problem Set|Response)\b',
    re.I
)


def parse_assignments_from_summary(course_id: str, course_name: str, summary_text: str) -> list[dict]:
    """
    Parse an AI-generated course summary text into a list of assignment dicts.
    Extracts the ASSIGNMENTS section and parses each individual block.
    """
    # Check for failed pre-processing
    if "pre-processing failed" in summary_text.lower():
        return []

    # Extract ASSIGNMENTS section (stop at next ## section)
    section_m = re.search(
        r'##\s+\d*\.?\s*ASSIGNMENTS?\b.*?\n(.*?)(?=\n##\s+\d*\.?\s*[A-Z]|\Z)',
        summary_text, re.I | re.S
    )
    section_text = section_m.group(1) if section_m else summary_text

    blocks = _parse_assignments_section(section_text)
    assignments = []

    for block in blocks:
        first_line = block.split('\n')[0]
        if not _ASSIGNMENT_HEADING_RE.match(first_line):
            continue

        # Heading title: strip ### and numbering prefix
        heading_title = re.sub(r'^#{1,4}\s+', '', first_line).strip()
        # Remove "Assignment 1:", "Assignment 1." etc. prefix
        heading_title = re.sub(
            r'^(?:Assignment|Exam|Quiz|Midterm|Final|Homework|Discussion|Paper|Project'
            r'|Presentation|Lab|Participation|Assessment|Test|Essay|Survey|Portfolio'
            r'|Writing|Reading Assignment|Problem Set|Response)\s*\d*\s*[:.]\s*',
            '', heading_title, flags=re.I
        ).strip()

        name       = _extract_field(block, "Name") or heading_title
        due_raw    = _extract_field(block, "Due Date", "Due date", "Due", "Deadline", "Date Due")
        weight     = _extract_field(block, "Weight", "Points", "Point Value",
                                    "% of Grade", "Percentage", "Value", "Grade Weight")
        atype      = _extract_field(block, "Type", "Assignment Type", "Format", "Category")
        confidence = _extract_field(block, "Confidence", "Confidence Score", "confidence")

        # Source links: almost never present in AI summaries, but check anyway
        urls = re.findall(r'https?://\S+', block)

        assignments.append({
            "title":           name,
            "course_id":       course_id,
            "course_name":     course_name,
            "due_date_raw":    due_raw,
            "assignment_type": atype,
            "point_value":     weight,
            "source_link":     urls[0] if urls else "",
            "confidence":      confidence,
        })

    return assignments


# ── Report helpers ────────────────────────────────────────────────────────────

def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "0.0%"


def _short_name(course_name: str, max_len: int = 55) -> str:
    name = re.sub(r'^Spring \d{4}\s+', '', course_name)
    name = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
    return name[:max_len]


def _h1(lines: list, text: str) -> None:
    lines += ["", "=" * 70, f"  {text}", "=" * 70]


# ── Main audit ────────────────────────────────────────────────────────────────

def run_audit(cache_path: Path) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_out = Path("output") / f"deadline_audit_{timestamp}.txt"

    # ── Load cache ────────────────────────────────────────────────────────────
    print(f"Loading {cache_path}…")
    with open(cache_path, encoding="utf-8") as f:
        raw_data = json.load(f)

    if not isinstance(raw_data, dict) or not all(isinstance(v, str) for v in raw_data.values()):
        print("Error: expected preprocessed cache format {course_id: summary_text}")
        sys.exit(1)

    # Resolve course names from paired content_text file
    course_names: dict[str, str] = {}
    stem = cache_path.stem  # e.g. preprocessed_content_text_20260514_084311
    paired_stem = re.sub(r'^preprocessed_', '', stem)
    paired_path = cache_path.parent / f"{paired_stem}.json"
    if paired_path.exists():
        with open(paired_path, encoding="utf-8") as f:
            content_data = json.load(f)
        for course in content_data.get("courses", []):
            course_names[course["course_id"]] = course["course_name"]

    summaries = {
        cid: {"name": course_names.get(cid, cid), "text": text}
        for cid, text in raw_data.items()
    }

    # ── Parse assignments ─────────────────────────────────────────────────────
    all_assignments: list[dict] = []
    failed_courses: list[str] = []

    for cid, info in summaries.items():
        if "pre-processing failed" in info["text"].lower():
            failed_courses.append(info["name"])
            continue
        parsed = parse_assignments_from_summary(cid, info["name"], info["text"])
        all_assignments.extend(parsed)

    total = len(all_assignments)

    # Classify dates and stash on each entry
    for a in all_assignments:
        cat = classify_date(a["due_date_raw"])
        a["_date_cat"] = cat
        a["_has_time"] = has_time(a["due_date_raw"], cat)

    # ── Build report ──────────────────────────────────────────────────────────
    lines: list[str] = []

    lines += [
        "=" * 70,
        "  BLACKBOARD DEADLINE AUDIT REPORT",
        f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Source    : {cache_path}",
        f"  Fuzzy lib : {FUZZY_LIB}  (threshold={DUPLICATE_THRESHOLD}%)",
        "=" * 70,
    ]

    if failed_courses:
        lines.append(f"\n  WARNING: {len(failed_courses)} course(s) had failed pre-processing and are excluded:")
        for cn in failed_courses:
            lines.append(f"    • {_short_name(cn)}")

    # ─────────────────────────────────────────────────────────────────────────
    # Section 1: TOTAL DEADLINES
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "1. TOTAL DEADLINES EXTRACTED")
    lines.append(f"\n  Overall: {total} deadline(s) across {len(summaries)} course(s)\n")

    course_counts = Counter(a["course_id"] for a in all_assignments)
    for cid, info in summaries.items():
        if "pre-processing failed" in info["text"].lower():
            continue
        count = course_counts.get(cid, 0)
        flag = "  ⚠  LOW — investigate" if count < 5 else ""
        lines.append(f"    {_short_name(info['name']):<55}  {count:>3}{flag}")

    # ─────────────────────────────────────────────────────────────────────────
    # Section 2: DATE FORMAT BREAKDOWN
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "2. DATE FORMAT BREAKDOWN")

    date_counts = Counter(a["_date_cat"] for a in all_assignments)
    cat_labels = {
        "iso_datetime": "ISO datetime  (date + time)",
        "iso_date":     "ISO date      (date only, no time)",
        "date_string":  "Date string   (natural language, parseable)",
        "relative":     "Relative      (Week N, next Tuesday, etc.)",
        "vague":        "Vague         (end of semester, TBD, etc.)",
        "missing":      "Missing       (no date field at all)",
    }

    lines.append("")
    for cat in ("iso_datetime", "iso_date", "date_string", "relative", "vague", "missing"):
        n = date_counts.get(cat, 0)
        bar = "█" * min(n, 35)
        lines.append(f"    {cat_labels[cat]:<44}  {n:>4}  ({_pct(n, total):>6})  {bar}")

    parseable = sum(date_counts.get(c, 0) for c in ("iso_datetime", "iso_date", "date_string"))
    parseable_pct = 100 * parseable / total if total else 0
    lines.append(
        f"\n  Parseable (iso_datetime + iso_date + date_string): "
        f"{parseable}/{total} ({parseable_pct:.1f}%)"
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Section 3: METADATA COMPLETENESS
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "3. METADATA COMPLETENESS")

    field_specs = [
        # (dict_key_or_special,  label,                               critical)
        ("title",           "Title (assignment name)",                True),
        ("course_name",     "Course (name / ID)",                     True),
        ("due_date_raw",    "Due date",                               True),
        ("_has_time",       "Due time or all-day flag",               False),
        ("assignment_type", "Assignment type (exam/paper/quiz/…)",    False),
        ("point_value",     "Point value / weight",                   False),
        ("source_link",     "Source link (Blackboard URL)",           False),
        ("confidence",      "Confidence score",                       False),
    ]

    lines.append("")
    missing_critical: list[str] = []
    for key, label, critical in field_specs:
        if key == "_has_time":
            missing_n = sum(1 for a in all_assignments if not a.get("_has_time"))
        else:
            missing_n = sum(1 for a in all_assignments if not a.get(key))
        flag = "  ← CRITICAL" if (critical and missing_n > 0) else ""
        lines.append(f"    {label:<44}  missing: {missing_n:>4} / {total}{flag}")
        if critical and missing_n > 0:
            missing_critical.append(label)

    # ─────────────────────────────────────────────────────────────────────────
    # Section 4: DUPLICATE DETECTION
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "4. DUPLICATE DETECTION")
    lines.append(f"\n  Checking same-course pairs with title similarity ≥ {DUPLICATE_THRESHOLD}%\n")

    duplicate_pairs: list[tuple[int, int, float]] = []
    for i in range(len(all_assignments)):
        for j in range(i + 1, len(all_assignments)):
            a, b = all_assignments[i], all_assignments[j]
            if a["course_id"] != b["course_id"]:
                continue
            sim = _similarity(a["title"], b["title"])
            if sim >= DUPLICATE_THRESHOLD:
                duplicate_pairs.append((i, j, sim))

    if duplicate_pairs:
        lines.append(f"  Found {len(duplicate_pairs)} potential duplicate pair(s):\n")
        for i, j, sim in duplicate_pairs[:20]:
            a, b = all_assignments[i], all_assignments[j]
            cname = _short_name(a["course_name"], 42)
            lines.append(f"    [{cname}]  similarity={sim:.0f}%")
            ad = (a["due_date_raw"] or "n/a")[:45]
            bd = (b["due_date_raw"] or "n/a")[:45]
            lines.append(f"      A: {a['title'][:62]}")
            lines.append(f"         due: {ad}")
            lines.append(f"      B: {b['title'][:62]}")
            lines.append(f"         due: {bd}")
            lines.append("")
        if len(duplicate_pairs) > 20:
            lines.append(f"  … {len(duplicate_pairs) - 20} more pair(s) not shown.")
    else:
        lines.append("  No duplicates detected.")

    # ─────────────────────────────────────────────────────────────────────────
    # Section 5: CONFIDENCE SCORES
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "5. CONFIDENCE SCORES")
    lines.append("")

    with_conf = [a for a in all_assignments if a.get("confidence")]
    if with_conf:
        lines.append(f"  Confidence field present in {len(with_conf)} / {total} entries.")
        samples = [a["confidence"] for a in with_conf[:8]]
        lines.append(f"  Sample values: {samples}")
    else:
        lines.append("  Confidence field: NOT PRESENT in any entry.")
        lines.append("")
        lines.append("  → ACTION NEEDED: Add confidence scoring to the extraction prompt in query.py.")
        lines.append("    Suggested addition to PREPROCESS_SYSTEM_PROMPT:")
        lines.append('    "For each assignment, include a Confidence field (1-5) rating how certain')
        lines.append('     you are about the due date and weight: 5=explicitly stated, 1=inferred."')

    # ─────────────────────────────────────────────────────────────────────────
    # Section 6: SAMPLE EXTRACTIONS
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "6. SAMPLE EXTRACTIONS  (10 random entries)")

    sample_size = min(10, total)
    sample = random.sample(all_assignments, sample_size) if total > 0 else []

    for idx, a in enumerate(sample, 1):
        lines.append(f"\n  [{idx:02d}] {a['title']}")
        lines.append(f"        Course   : {_short_name(a['course_name'])}")
        lines.append(f"        Due      : {a['due_date_raw'] or '(missing)'}")
        lines.append(f"        Date cat : {a.get('_date_cat', '?')}")
        lines.append(f"        Type     : {a['assignment_type'] or '(missing)'}")
        lines.append(f"        Weight   : {a['point_value'] or '(missing)'}")
        lines.append(f"        URL      : {a['source_link'] or '(none)'}")
        lines.append(f"        Has time : {'yes' if a.get('_has_time') else 'no'}")

    if total == 0:
        lines.append("\n  (No assignments parsed — check parser output above.)")

    # ─────────────────────────────────────────────────────────────────────────
    # Section 7: SUMMARY VERDICT
    # ─────────────────────────────────────────────────────────────────────────
    _h1(lines, "7. SUMMARY VERDICT")
    lines.append("")
    lines.append(f"  Deadlines extracted  : {total}")
    lines.append(f"  Parseable dates      : {parseable}/{total} ({parseable_pct:.1f}%)")
    lines.append(f"  Critical gaps        : {', '.join(missing_critical) if missing_critical else 'none'}")
    lines.append(f"  Duplicate pairs      : {len(duplicate_pairs)}")
    lines.append(f"  Confidence scores    : {'present' if with_conf else 'absent — needs prompt update'}")
    lines.append("")

    # Determine verdict
    has_critical_gaps = bool(missing_critical)
    has_duplicates = len(duplicate_pairs) > 0

    if parseable_pct >= 80 and not has_critical_gaps and not has_duplicates:
        verdict = "GREEN"
        verdict_note = (
            f">80% of dates parseable ({parseable_pct:.0f}%), no critical metadata gaps, "
            f"no duplicates → READY FOR CALENDAR"
        )
    elif parseable_pct >= 60:
        verdict = "YELLOW"
        reasons = []
        if parseable_pct < 80:
            reasons.append(f"only {parseable_pct:.0f}% parseable (need >80%)")
        if has_critical_gaps:
            reasons.append(f"critical gaps: {', '.join(missing_critical)}")
        if has_duplicates:
            reasons.append(f"{len(duplicate_pairs)} duplicate pair(s)")
        verdict_note = "Fix extraction prompt before building calendar: " + "; ".join(reasons)
    else:
        verdict = "RED"
        reasons = []
        reasons.append(f"only {parseable_pct:.0f}% of dates parseable (threshold 60%)")
        if has_critical_gaps:
            reasons.append(f"critical gaps: {', '.join(missing_critical)}")
        verdict_note = "Significant prompt engineering needed: " + "; ".join(reasons)

    # ANSI colours for terminal only (stripped when writing file)
    _C = {"GREEN": "\033[32m", "YELLOW": "\033[33m", "RED": "\033[31m"}
    _R = "\033[0m"
    color = _C.get(verdict, "")
    lines.append(f"  {color}{'▌' * 3}  VERDICT: {verdict}  {'▌' * 3}{_R}")
    lines.append(f"  {verdict_note}")
    lines.append("")

    # ── Output ────────────────────────────────────────────────────────────────
    report = "\n".join(lines)
    print(report)

    # Strip ANSI codes for file output
    clean = re.sub(r'\x1b\[[0-9;]*m', '', report)
    audit_out.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_out, "w", encoding="utf-8") as f:
        f.write(clean)

    print(f"\n  Report saved → {audit_out}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def find_most_recent_preprocessed() -> Path:
    output_dir = Path("output")
    candidates = sorted(
        output_dir.glob("preprocessed_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        print("Error: no preprocessed_*.json files found in output/")
        sys.exit(1)
    return candidates[0]


if __name__ == "__main__":
    if len(sys.argv) >= 2:
        path = Path(sys.argv[1])
        if not path.exists():
            print(f"Error: not found: {path}")
            sys.exit(1)
    else:
        path = find_most_recent_preprocessed()
        print(f"Auto-detected cache: {path}\n")

    run_audit(path)
