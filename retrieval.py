"""
retrieval.py — cosine-similarity search over stored chunk embeddings.

Embeds the query via llm_adapter.embed, fetches candidate chunks from
documents_helper.list_chunks (with_embedding_only=True), computes cosine
similarity via numpy, and returns the top-k results.

Fail-open: any error at any stage returns [] instead of raising.
"""

import numpy as np

import documents_helper
from llm_adapter import embed

DEFAULT_TOP_K = 8


def search(
    query: str,
    user_id: str | None = None,
    school_id: str | None = None,
    course_id: str | None = None,
    content_type: str | None = None,
    source_type: str | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> list[dict]:
    """Search stored chunks by semantic similarity to *query*.

    Embeds the query locally, fetches all candidate chunks that pass the
    optional filters and have an embedding stored, computes cosine similarity
    via numpy, and returns the top *top_k* results sorted descending by score.

    Returns [] on any failure (fail-open).

    Each result dict contains:
        chunk_id, document_id, text, score (float), metadata
    """
    # 1. Embed the query.
    try:
        query_vec = np.array(embed([query])[0], dtype=np.float64)
    except Exception:
        return []

    q_norm = np.linalg.norm(query_vec)
    if q_norm == 0.0:
        return []
    query_unit = query_vec / q_norm

    # 2. Fetch candidates.
    try:
        candidates = documents_helper.list_chunks(
            user_id=user_id,
            school_id=school_id,
            course_id=course_id,
            content_type=content_type,
            source_type=source_type,
            with_embedding_only=True,
        )
    except Exception:
        return []

    if not candidates:
        return []

    # 3. Build embedding matrix; drop any chunk whose vector is malformed.
    valid_chunks: list[dict] = []
    vectors: list[np.ndarray] = []
    for chunk in candidates:
        try:
            vec = np.array(chunk["embedding"], dtype=np.float64)
            if vec.ndim != 1 or vec.shape[0] == 0:
                continue
            norm = np.linalg.norm(vec)
            if norm == 0.0:
                continue
            valid_chunks.append(chunk)
            vectors.append(vec / norm)
        except Exception:
            continue

    if not valid_chunks:
        return []

    # 4. Cosine similarity = dot product of unit vectors.
    matrix = np.stack(vectors)          # shape (N, D)
    scores = matrix @ query_unit        # shape (N,)

    # 5. Sort descending, take top_k.
    top_indices = np.argsort(scores)[::-1][:top_k]

    results: list[dict] = []
    for idx in top_indices:
        chunk = valid_chunks[idx]
        results.append({
            "chunk_id": chunk["id"],
            "document_id": chunk["document_id"],
            "text": chunk["text"],
            "score": float(scores[idx]),
            "metadata": chunk.get("metadata", {}),
        })

    return results
