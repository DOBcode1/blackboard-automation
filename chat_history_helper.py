"""
chat_history_helper.py — persistence layer for chat thread history.

Mirrors overrides_helper.py: atomic writes via tempfile + os.replace,
functions never mutate caller data.

Data lives at output/chat_history.json with structure:
{
  "user_id": "local_dev",
  "school_id": "fordham",
  "settings": {"memory_enabled": true},
  "threads": [...],
  "memory_summaries": []
}
"""

import copy
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

_DEFAULT_PATH = Path("output/chat_history.json")

_EMPTY: dict = {
    "user_id": "local_dev",
    "school_id": "fordham",
    "settings": {"memory_enabled": True},
    "threads": [],
    "memory_summaries": [],
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    """Parse ISO timestamp, returning timezone-aware datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_history(path: Path = _DEFAULT_PATH) -> dict:
    """Load chat history; returns empty skeleton on any error."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data.get("threads"), list):
            raise ValueError("Missing or invalid 'threads' key")
        return data
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY)
    except Exception as exc:
        print(f"WARNING: could not load {path}: {exc}", file=sys.stderr)
        return copy.deepcopy(_EMPTY)


def save_history(data: dict, path: Path = _DEFAULT_PATH) -> None:
    """Write chat history atomically (tempfile + os.replace) with 2-space indent."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
# Utility
# ---------------------------------------------------------------------------

def auto_title_from_message(content: str) -> str:
    """Generate a thread title (~50 chars) from the first line of a message."""
    title = content.strip().replace("\n", " ").replace("\r", "")
    return (title[:49] + "…") if len(title) > 50 else title


# ---------------------------------------------------------------------------
# Thread operations
# ---------------------------------------------------------------------------

def create_thread(title: str | None = None, incognito: bool = False) -> str:
    """
    Create a new thread and return its ID.
    incognito=True threads are NOT saved to disk; caller manages in memory.
    Returns thread_id regardless.
    """
    tid = f"thread_{int(time.time() * 1000)}"
    now = _now_iso()
    thread = {
        "id": tid,
        "title": title or "New chat",
        "created_at": now,
        "updated_at": now,
        "incognito": incognito,
        "deleted_at": None,
        "messages": [],
        "documents": [],
    }
    if not incognito:
        data = load_history()
        data["threads"].insert(0, thread)
        save_history(data)
    return tid


def get_thread(thread_id: str) -> dict | None:
    """Return thread dict or None if not found."""
    data = load_history()
    for t in data["threads"]:
        if t["id"] == thread_id:
            return copy.deepcopy(t)
    return None


def list_threads(include_deleted: bool = False) -> list:
    """Return threads sorted by updated_at desc."""
    data = load_history()
    result = data["threads"]
    if not include_deleted:
        result = [t for t in result if t.get("deleted_at") is None]
    return sorted(result, key=lambda t: t.get("updated_at", ""), reverse=True)


def append_message(thread_id: str, role: str, content: str) -> None:
    """
    Append a message to a persistent thread.
    No-op if memory_enabled is False.
    Auto-titles the thread from the first user message.
    """
    data = load_history()
    if not data.get("settings", {}).get("memory_enabled", True):
        return
    now = _now_iso()
    for thread in data["threads"]:
        if thread["id"] == thread_id:
            if role == "user" and not thread["messages"] and thread["title"] == "New chat":
                thread["title"] = auto_title_from_message(content)
            thread["messages"].append({"role": role, "content": content, "timestamp": now})
            thread["updated_at"] = now
            save_history(data)
            return
    print(f"WARNING: thread not found for append_message: {thread_id}", file=sys.stderr)


def soft_delete_thread(thread_id: str) -> None:
    """Mark thread as deleted (sets deleted_at timestamp)."""
    data = load_history()
    for t in data["threads"]:
        if t["id"] == thread_id:
            t["deleted_at"] = _now_iso()
            save_history(data)
            return


def restore_thread(thread_id: str) -> None:
    """Restore a soft-deleted thread."""
    data = load_history()
    for t in data["threads"]:
        if t["id"] == thread_id:
            t["deleted_at"] = None
            t["updated_at"] = _now_iso()
            save_history(data)
            return


def rename_thread(thread_id: str, title: str) -> None:
    """Rename a thread (truncated to 50 chars)."""
    data = load_history()
    for t in data["threads"]:
        if t["id"] == thread_id:
            t["title"] = title[:50]
            t["updated_at"] = _now_iso()
            save_history(data)
            return


def purge_old_deleted(days: int = 30) -> int:
    """Permanently remove threads soft-deleted more than `days` ago. Returns count removed."""
    data = load_history()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def is_purgeable(t: dict) -> bool:
        deleted_at = t.get("deleted_at")
        if not deleted_at:
            return False
        try:
            return _parse_iso(deleted_at) < cutoff
        except (ValueError, TypeError):
            return False

    before = len(data["threads"])
    data["threads"] = [t for t in data["threads"] if not is_purgeable(t)]
    purged = before - len(data["threads"])
    if purged > 0:
        save_history(data)
    return purged


def purge_thread(thread_id: str) -> bool:
    """Permanently remove a specific thread. Returns True if found and removed."""
    data = load_history()
    before = len(data["threads"])
    data["threads"] = [t for t in data["threads"] if t["id"] != thread_id]
    if len(data["threads"]) < before:
        save_history(data)
        return True
    return False


def clear_all_threads() -> int:
    """Soft-delete all active threads. Returns count deleted."""
    data = load_history()
    now = _now_iso()
    count = 0
    for t in data["threads"]:
        if t.get("deleted_at") is None:
            t["deleted_at"] = now
            count += 1
    if count > 0:
        save_history(data)
    return count


def get_settings() -> dict:
    """Return a copy of the settings dict."""
    data = load_history()
    return copy.deepcopy(data.get("settings", {"memory_enabled": True}))


def set_memory_enabled(enabled: bool) -> None:
    """Toggle memory persistence for new messages."""
    data = load_history()
    data.setdefault("settings", {})["memory_enabled"] = enabled
    save_history(data)
