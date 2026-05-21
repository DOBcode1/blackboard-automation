"""
overrides_helper.py — shared module for reading, writing, and applying
user_overrides.json. Import this from aggregate_deadlines.py, app.py, and
query.py. Never mutates caller data; all functions return new dicts.
"""

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_PATH = Path("output/user_overrides.json")
_EMPTY: dict = {"version": 1, "overrides": {}}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_overrides(path: Path = _DEFAULT_PATH) -> dict:
    """Load and return the overrides dict; returns empty skeleton on any error."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data.get("overrides"), dict):
            raise ValueError("Missing or invalid 'overrides' key")
        return data
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY)
    except Exception as exc:
        print(f"WARNING: could not load {path}: {exc}", file=sys.stderr)
        return copy.deepcopy(_EMPTY)


def save_overrides(data: dict, path: Path = _DEFAULT_PATH) -> None:
    """Write overrides atomically (tempfile + os.replace) with 2-space indent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve _comment if already present in target but not in data
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if "_comment" in existing and "_comment" not in data:
            data = {"_comment": existing["_comment"], **data}
    except Exception:
        pass

    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Single-entry accessors
# ---------------------------------------------------------------------------

def get_override(overrides: dict, deadline_id: str) -> dict | None:
    """Return the override entry for deadline_id, or None if absent."""
    return overrides.get("overrides", {}).get(deadline_id)


def apply_override_to_deadline(deadline: dict, override: dict | None) -> dict:
    """Return a new deadline dict with the override applied; input is never mutated."""
    result = copy.deepcopy(deadline)
    if override is None:
        return result

    if override.get("dismissed", False):
        result["dismissed"] = True

    edits: dict = override.get("edits", {})
    edited_fields: list[str] = []
    ai_original: dict = {}

    for field, value in edits.items():
        if value is not None:
            ai_original[field] = result.get(field)
            result[field] = value
            edited_fields.append(field)

    if edited_fields:
        result["user_edited"] = True
        result["user_edited_fields"] = edited_fields
        result["ai_original"] = ai_original

    return result


# ---------------------------------------------------------------------------
# Mutation helpers (return modified overrides dict; caller must save)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_entry(overrides: dict, deadline_id: str) -> dict:
    """Return a deep copy of overrides with a guaranteed entry for deadline_id."""
    overrides = copy.deepcopy(overrides)
    overrides.setdefault("overrides", {})
    overrides["overrides"].setdefault(
        deadline_id,
        {"edits": {}, "dismissed": False, "edited_at": _now_iso(), "manual_add": False},
    )
    return overrides


def set_override(overrides: dict, deadline_id: str, field: str, value) -> dict:
    """Set a single field override on a deadline; creates the entry if absent."""
    overrides = _ensure_entry(overrides, deadline_id)
    overrides["overrides"][deadline_id]["edits"][field] = value
    overrides["overrides"][deadline_id]["edited_at"] = _now_iso()
    return overrides


def set_dismissed(overrides: dict, deadline_id: str, dismissed: bool = True) -> dict:
    """Toggle the dismissed flag for a deadline."""
    overrides = _ensure_entry(overrides, deadline_id)
    overrides["overrides"][deadline_id]["dismissed"] = dismissed
    overrides["overrides"][deadline_id]["edited_at"] = _now_iso()
    return overrides


def clear_override(overrides: dict, deadline_id: str, field: str | None = None) -> dict:
    """Remove a field override, or the entire entry if field is None."""
    overrides = copy.deepcopy(overrides)
    entries: dict = overrides.get("overrides", {})
    if deadline_id not in entries:
        return overrides

    if field is None:
        del entries[deadline_id]
        return overrides

    entry = entries[deadline_id]
    entry.get("edits", {}).pop(field, None)

    # Clean up empty entry
    if not entry.get("edits") and not entry.get("dismissed", False):
        del entries[deadline_id]

    return overrides


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import pprint

    ok = True

    def check(label: str, condition: bool) -> None:
        global ok
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}", file=sys.stderr)
            ok = False

    print("=== overrides_helper smoke test ===\n")

    # 1. Load from nonexistent path → empty skeleton
    empty = load_overrides(Path("output/__nonexistent__.json"))
    check("load missing file → version=1", empty["version"] == 1)
    check("load missing file → empty overrides", empty["overrides"] == {})

    # 2. Fake deadline
    deadline = {
        "id": "dl_abc123",
        "title": "Midterm Exam",
        "due_date_resolved": "2026-03-15T23:59:00",
        "type": "Exam",
        "course": "CISC 1234",
    }

    # 3. apply_override_to_deadline with None → unchanged
    result = apply_override_to_deadline(deadline, None)
    check("no override → unchanged title", result["title"] == "Midterm Exam")
    check("no override → no user_edited key", "user_edited" not in result)

    # 4. set_override chain
    ov = copy.deepcopy(empty)
    ov = set_override(ov, "dl_abc123", "title", "Midterm (rescheduled)")
    ov = set_override(ov, "dl_abc123", "due_date_resolved", "2026-03-20T23:59:00")
    ov = set_override(ov, "dl_abc123", "notes", "Prof moved it back one week")

    entry = get_override(ov, "dl_abc123")
    check("set_override creates entry", entry is not None)
    check("set_override title", entry["edits"]["title"] == "Midterm (rescheduled)")
    check("set_override due_date", entry["edits"]["due_date_resolved"] == "2026-03-20T23:59:00")
    check("set_override notes", entry["edits"]["notes"] == "Prof moved it back one week")
    check("edited_at present", "edited_at" in entry)

    # 5. apply_override_to_deadline with real override
    applied = apply_override_to_deadline(deadline, entry)
    check("applied title overridden", applied["title"] == "Midterm (rescheduled)")
    check("applied due_date overridden", applied["due_date_resolved"] == "2026-03-20T23:59:00")
    check("user_edited True", applied.get("user_edited") is True)
    check("user_edited_fields has title", "title" in applied.get("user_edited_fields", []))
    check("ai_original preserves old title", applied["ai_original"]["title"] == "Midterm Exam")
    check("input deadline not mutated", deadline["title"] == "Midterm Exam")

    print()
    print("Applied deadline:")
    pprint.pprint(applied)

    # 6. set_dismissed
    ov2 = set_dismissed(ov, "dl_abc123", True)
    entry2 = get_override(ov2, "dl_abc123")
    check("dismissed set to True", entry2["dismissed"] is True)

    dismissed_dl = apply_override_to_deadline(deadline, entry2)
    check("apply dismissed → dismissed=True on result", dismissed_dl.get("dismissed") is True)

    ov2 = set_dismissed(ov2, "dl_abc123", False)
    check("dismissed toggled back", get_override(ov2, "dl_abc123")["dismissed"] is False)

    # 7. clear_override single field
    ov3 = clear_override(ov, "dl_abc123", "title")
    entry3 = get_override(ov3, "dl_abc123")
    check("clear title → title gone from edits", "title" not in entry3.get("edits", {}))
    check("clear title → other fields remain", "notes" in entry3.get("edits", {}))

    # 8. clear_override full entry
    ov4 = clear_override(ov, "dl_abc123")
    check("clear full entry → gone", get_override(ov4, "dl_abc123") is None)

    # 9. clear_override removes entry when edits empty + not dismissed
    ov5 = set_override(copy.deepcopy(empty), "dl_xyz", "title", "Only field")
    ov5 = clear_override(ov5, "dl_xyz", "title")
    check("auto-remove entry when edits empty + not dismissed", get_override(ov5, "dl_xyz") is None)

    # 10. No disk writes occurred
    print()
    print("=== All checks complete (no disk writes) ===")
    sys.exit(0 if ok else 1)
