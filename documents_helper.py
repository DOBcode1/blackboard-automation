"""
documents_helper.py — persistence layer for document records and chunks.

Mirrors chat_history_helper.py: atomic writes via tempfile + os.replace,
functions never mutate caller data (return deep copies).

Document data lives at output/documents.json with structure:
{
  "user_id": "local_dev",
  "school_id": "fordham",
  "documents": [...]
}

Chunk data lives at output/document_chunks.json with structure:
{
  "user_id": "local_dev",
  "school_id": "fordham",
  "chunks": [...]
}
"""

import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

_DEFAULT_PATH = Path("output/documents.json")
_DEFAULT_CHUNKS_PATH = Path("output/document_chunks.json")

_EMPTY: dict = {
    "user_id": "local_dev",
    "school_id": "fordham",
    "documents": [],
}

_EMPTY_CHUNKS: dict = {
    "user_id": "local_dev",
    "school_id": "fordham",
    "chunks": [],
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


# ---------------------------------------------------------------------------
# Chunk I/O
# ---------------------------------------------------------------------------

def load_chunks(path: Path = _DEFAULT_CHUNKS_PATH) -> dict:
    """Load chunk store; returns empty skeleton on any error."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data.get("chunks"), list):
            raise ValueError("Missing or invalid 'chunks' key")
        return data
    except FileNotFoundError:
        return copy.deepcopy(_EMPTY_CHUNKS)
    except Exception as exc:
        print(f"WARNING: could not load {path}: {exc}", file=sys.stderr)
        return copy.deepcopy(_EMPTY_CHUNKS)


def save_chunks(data: dict, path: Path = _DEFAULT_CHUNKS_PATH) -> None:
    """Write chunk store atomically (tempfile + os.replace) with 2-space indent."""
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
# Chunk operations
# ---------------------------------------------------------------------------

def add_chunks(document_id: str, chunks: list[dict]) -> list[dict]:
    """
    Build full chunk records for each entry in `chunks` and append them to the store.

    Each input dict must contain at least ``chunk_index`` and ``text``.
    Optional keys: ``embedding`` (default None) and ``metadata`` (overrides
    the metadata derived from the parent document).

    Looks up the parent document; raises ValueError if it does not exist.
    Increments the parent document's chunk_count and refreshes updated_at.
    Performs at most one save to each store.

    Returns deep copies of the created chunk records.
    """
    doc = get_document(document_id)
    if doc is None:
        raise ValueError(f"Document not found: {document_id!r}")

    parent_metadata = {
        "source_type": doc.get("source_type"),
        "course_id": doc.get("course_id"),
        "content_type": doc.get("content_type"),
        "semester_id": doc.get("semester_id"),
    }

    now = _now_iso()
    created: list[dict] = []
    for chunk_in in chunks:
        record: dict[str, Any] = {
            "id": f"chunk_{uuid4().hex}",
            "document_id": document_id,
            "user_id": doc["user_id"],
            "school_id": doc["school_id"],
            "chunk_index": chunk_in["chunk_index"],
            "text": chunk_in["text"],
            "embedding": chunk_in.get("embedding", None),
            "metadata": copy.deepcopy(chunk_in["metadata"]) if "metadata" in chunk_in else copy.deepcopy(parent_metadata),
            "created_at": now,
        }
        created.append(record)

    chunk_data = load_chunks()
    chunk_data["chunks"].extend(created)
    save_chunks(chunk_data)

    # Update parent document's chunk_count and updated_at (one save).
    doc_data = load_documents()
    for d in doc_data["documents"]:
        if d["id"] == document_id:
            d["chunk_count"] = d.get("chunk_count", 0) + len(created)
            d["updated_at"] = _now_iso()
            break
    save_documents(doc_data)

    return copy.deepcopy(created)


def get_chunks_for_document(document_id: str) -> list[dict]:
    """Return deep copies of all chunks for a document, sorted by chunk_index."""
    data = load_chunks()
    results = [c for c in data["chunks"] if c.get("document_id") == document_id]
    results.sort(key=lambda c: c.get("chunk_index", 0))
    return copy.deepcopy(results)


def list_chunks(
    user_id: str | None = None,
    school_id: str | None = None,
    course_id: str | None = None,
    content_type: str | None = None,
    source_type: str | None = None,
    with_embedding_only: bool = False,
) -> list[dict]:
    """
    Return deep copies of chunks matching all provided filters.

    ``user_id`` and ``school_id`` match top-level chunk fields.
    ``course_id``, ``content_type``, and ``source_type`` match values inside
    the chunk's ``metadata`` dict.
    A filter argument that is None is not applied.
    When ``with_embedding_only`` is True, chunks with a None or empty
    embedding are excluded.
    """
    data = load_chunks()
    results = data["chunks"]

    if user_id is not None:
        results = [c for c in results if c.get("user_id") == user_id]
    if school_id is not None:
        results = [c for c in results if c.get("school_id") == school_id]
    if course_id is not None:
        results = [c for c in results if (c.get("metadata") or {}).get("course_id") == course_id]
    if content_type is not None:
        results = [c for c in results if (c.get("metadata") or {}).get("content_type") == content_type]
    if source_type is not None:
        results = [c for c in results if (c.get("metadata") or {}).get("source_type") == source_type]
    if with_embedding_only:
        results = [c for c in results if c.get("embedding")]

    return copy.deepcopy(results)


def set_chunk_embeddings(embeddings_by_id: dict[str, Any]) -> int:
    """
    Set embeddings on chunks by ID.

    ``embeddings_by_id`` maps chunk id → embedding vector.
    Saves the chunk store once regardless of how many chunks were updated.
    Returns the count of chunks that were updated.
    """
    data = load_chunks()
    updated = 0
    for chunk in data["chunks"]:
        if chunk["id"] in embeddings_by_id:
            chunk["embedding"] = embeddings_by_id[chunk["id"]]
            updated += 1
    if updated > 0:
        save_chunks(data)
    return updated


def delete_chunks_for_document(document_id: str) -> int:
    """
    Hard-remove all chunks belonging to ``document_id`` (single save).

    Also resets the parent document's chunk_count to 0 and refreshes
    updated_at if the parent document exists.
    Returns the number of chunks removed.
    """
    chunk_data = load_chunks()
    before = len(chunk_data["chunks"])
    chunk_data["chunks"] = [c for c in chunk_data["chunks"] if c.get("document_id") != document_id]
    removed = before - len(chunk_data["chunks"])
    if removed > 0:
        save_chunks(chunk_data)

    doc_data = load_documents()
    for d in doc_data["documents"]:
        if d["id"] == document_id:
            d["chunk_count"] = 0
            d["updated_at"] = _now_iso()
            save_documents(doc_data)
            break

    return removed


def purge_old_deleted_documents(days: int = 30) -> int:
    """
    Permanently remove documents whose deleted_at is more than ``days`` ago,
    along with all chunks belonging to those documents.

    Returns the count of documents purged.
    """
    doc_data = load_documents()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def is_purgeable(d: dict) -> bool:
        deleted_at = d.get("deleted_at")
        if not deleted_at:
            return False
        try:
            return _parse_iso(deleted_at) < cutoff
        except (ValueError, TypeError):
            return False

    purgeable_ids = {d["id"] for d in doc_data["documents"] if is_purgeable(d)}
    if not purgeable_ids:
        return 0

    doc_data["documents"] = [d for d in doc_data["documents"] if d["id"] not in purgeable_ids]
    save_documents(doc_data)

    chunk_data = load_chunks()
    chunk_data["chunks"] = [c for c in chunk_data["chunks"] if c.get("document_id") not in purgeable_ids]
    save_chunks(chunk_data)

    return len(purgeable_ids)
