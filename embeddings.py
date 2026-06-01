"""
embeddings.py — orchestrates embedding of document chunks.

Reads chunks via documents_helper, calls llm_adapter.embed in batches,
and writes results back via documents_helper.
"""

import time

import documents_helper
from llm_adapter import embed, EMBEDDING_MODEL

DEFAULT_BATCH_SIZE = 64


def _embed_with_retry(texts: list[str], max_retries: int = 3) -> list[list[float]]:
    """Call embed(texts) with exponential backoff on failure.

    Retries up to max_retries times (starting at ~1 s, doubling each attempt).
    Re-raises on the final failure.
    """
    delay = 1.0
    for attempt in range(max_retries):
        try:
            return embed(texts)
        except Exception:
            if attempt == max_retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("Retry loop exited unexpectedly")  # unreachable


def embed_document_chunks(
    document_id: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict:
    """Embed all un-embedded chunks for a document and persist the vectors.

    Processes chunks in batches. A batch that fails after retries is skipped
    (those chunks keep embedding=None) rather than aborting the whole document.

    Returns a summary dict with keys:
        document_id, total_pending, embedded, failed, status
    """
    chunks = documents_helper.get_chunks_for_document(document_id)
    pending = [c for c in chunks if not c.get("embedding")]

    total_pending = len(pending)
    embedded_count = 0
    failed_count = 0

    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [c["text"] for c in batch]
        chunk_ids = [c["id"] for c in batch]

        try:
            vectors = _embed_with_retry(texts)
            embeddings_by_id = dict(zip(chunk_ids, vectors))
            documents_helper.set_chunk_embeddings(embeddings_by_id)
            embedded_count += len(batch)
        except Exception:
            failed_count += len(batch)

    # Determine final status: "embedded" if any chunk now has a vector, else "failed".
    all_chunks = documents_helper.get_chunks_for_document(document_id)
    has_any_embedding = any(c.get("embedding") for c in all_chunks)
    status = "embedded" if has_any_embedding else "failed"

    documents_helper.update_document(
        document_id,
        embeddings_status=status,
        embedding_model=EMBEDDING_MODEL if status == "embedded" else None,
    )

    return {
        "document_id": document_id,
        "total_pending": total_pending,
        "embedded": embedded_count,
        "failed": failed_count,
        "status": status,
    }


def embed_all_pending(batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    """Embed chunks for every document whose embeddings_status is 'pending'.

    Returns an aggregate summary dict with keys:
        documents_processed, chunks_embedded, chunks_failed
    """
    pending_docs = [
        d for d in documents_helper.list_documents()
        if d.get("embeddings_status") == "pending"
    ]

    documents_processed = 0
    chunks_embedded = 0
    chunks_failed = 0

    for doc in pending_docs:
        result = embed_document_chunks(doc["id"], batch_size=batch_size)
        documents_processed += 1
        chunks_embedded += result["embedded"]
        chunks_failed += result["failed"]

    return {
        "documents_processed": documents_processed,
        "chunks_embedded": chunks_embedded,
        "chunks_failed": chunks_failed,
    }
