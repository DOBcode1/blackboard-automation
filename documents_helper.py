"""
documents_helper.py — persistence layer for document records.

Mirrors chat_history_helper.py: atomic writes via tempfile + os.replace,
functions never mutate caller data (return deep copies).

Data lives at output/documents.json with structure:
{
  "user_id": "local_dev",
  "school_id": "fordham",
  "documents": [...]
}
"""

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

_DEFAULT_PATH = Path("output/documents.json")

_EMPTY: dict = {
    "user_id": "local_dev",
    "school_id": "fordham",
    "documents": [],
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def load_documents(path: Path = _DEFAULT_PATH) -> dict:
    """Load documents store; returns empty skeleton on any error."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data.get("documents"), list):
            raise ValueError("Missing or invalid 'documents' key")
        return data
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY)
    except Exception as exc:
        print(f"WARNING: could not load {path}: {exc}", file=sys.stderr)
        return copy.deepcopy(_EMPTY)


def save_documents(data: dict, path: Path = _DEFAULT_PATH) -> None:
    """Write documents store atomically (tempfile + os.replace) with 2-space indent."""
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
# Document CRUD
# ---------------------------------------------------------------------------

def create_document(
    *,
    title: str,
    extracted_text: str,
    source_type: str,
    extraction_method: str,
    content_type: str = "other",
    user_id: str = "local_dev",
    school_id: str = "fordham",
    course_id: str | None = None,
    topic: str | None = None,
    original_filename: str | None = None,
    extraction_confidence: int = 5,
    original_file_path: str | None = None,
    mime_type: str | None = None,
    file_size_bytes: int | None = None,
    thread_id: str | None = None,
    semester_id: str | None = None,
    user_provided_metadata: dict | None = None,
) -> dict:
    """
    Create a new document record, append it to the store, and save.

    Returns a deep copy of the created record.
    """
    now = _now_iso()
    doc: dict[str, Any] = {
        "id": f"doc_{uuid4().hex}",
        "user_id": user_id,
        "school_id": school_id,
        "source_type": source_type,
        "course_id": course_id,
        "topic": topic,
        "title": title,
        "original_filename": original_filename,
        "content_type": content_type,
        "extracted_text": extracted_text,
        "extraction_method": extraction_method,
        "extraction_confidence": extraction_confidence,
        "original_file_path": original_file_path,
        "mime_type": mime_type,
        "file_size_bytes": file_size_bytes,
        "thread_id": thread_id,
        "semester_id": semester_id,
        "embeddings_status": "pending",
        "embedding_model": None,
        "chunk_count": 0,
        "user_provided_metadata": user_provided_metadata if user_provided_metadata is not None else {},
        "created_at": now,
        "updated_at": now,
        "deleted_at": None,
    }
    data = load_documents()
    data["documents"].append(doc)
    save_documents(data)
    return copy.deepcopy(doc)


def get_document(doc_id: str) -> dict | None:
    """Return a deep copy of the document record, or None if not found."""
    data = load_documents()
    for doc in data["documents"]:
        if doc["id"] == doc_id:
            return copy.deepcopy(doc)
    return None


def list_documents(
    user_id: str | None = None,
    school_id: str | None = None,
    course_id: str | None = None,
    source_type: str | None = None,
    content_type: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    """
    Return deep copies of documents matching all provided filters.

    A filter argument that is None is not applied.
    By default, documents with a non-null deleted_at are excluded.
    """
    data = load_documents()
    results = data["documents"]

    if not include_deleted:
        results = [d for d in results if d.get("deleted_at") is None]
    if user_id is not None:
        results = [d for d in results if d.get("user_id") == user_id]
    if school_id is not None:
        results = [d for d in results if d.get("school_id") == school_id]
    if course_id is not None:
        results = [d for d in results if d.get("course_id") == course_id]
    if source_type is not None:
        results = [d for d in results if d.get("source_type") == source_type]
    if content_type is not None:
        results = [d for d in results if d.get("content_type") == content_type]

    return copy.deepcopy(results)


_IMMUTABLE_FIELDS = {"id", "created_at"}


def update_document(doc_id: str, **fields: Any) -> dict | None:
    """
    Update fields on a document record and refresh updated_at.

    id and created_at cannot be overwritten.
    Returns the updated deep copy, or None if not found.
    """
    for key in _IMMUTABLE_FIELDS:
        fields.pop(key, None)

    data = load_documents()
    for doc in data["documents"]:
        if doc["id"] == doc_id:
            doc.update(fields)
            doc["updated_at"] = _now_iso()
            save_documents(data)
            return copy.deepcopy(doc)
    return None


def soft_delete_document(doc_id: str) -> None:
    """Mark a document as deleted by setting deleted_at to the current timestamp."""
    data = load_documents()
    for doc in data["documents"]:
        if doc["id"] == doc_id:
            doc["deleted_at"] = _now_iso()
            save_documents(data)
            return


def restore_document(doc_id: str) -> None:
    """Restore a soft-deleted document by clearing deleted_at and refreshing updated_at."""
    data = load_documents()
    for doc in data["documents"]:
        if doc["id"] == doc_id:
            doc["deleted_at"] = None
            doc["updated_at"] = _now_iso()
            save_documents(data)
            return
