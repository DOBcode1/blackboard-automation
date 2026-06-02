"""
ingestion.py — core text-ingestion pipeline.

Turns already-extracted text into a fully embedded document:
  create_document -> chunk_text -> add_chunks -> embed_document_chunks

Stage B (PDF/image/DOCX extractors) will call ingest_text after pulling
text out of binary files. This module handles only the storage pipeline.
"""

import documents_helper
import embeddings as embeddings_mod
from chunking import chunk_text


def ingest_text(
    extracted_text: str | None,
    *,
    title: str,
    source_type: str,
    extraction_method: str,
    content_type: str = "other",
    course_id: str | None = None,
    semester_id: str | None = None,
    user_provided_metadata: dict | None = None,
    **doc_fields,
) -> str | None:
    """Ingest already-extracted text into the document store as a fully embedded document.

    Steps:
      1. Guard: return None if text is empty/whitespace.
      2. create_document with all provided metadata.
      3. chunk_text -> add_chunks.
      4. embed_document_chunks (batched, fail-graceful).
      5. Return the new document id.

    Extra keyword arguments in ``doc_fields`` are forwarded to create_document
    (e.g. original_filename, topic, mime_type).

    Returns None if there is nothing to ingest.
    """
    if not extracted_text or not extracted_text.strip():
        return None

    doc = documents_helper.create_document(
        title=title,
        extracted_text=extracted_text,
        source_type=source_type,
        extraction_method=extraction_method,
        content_type=content_type,
        course_id=course_id,
        semester_id=semester_id,
        user_provided_metadata=user_provided_metadata if user_provided_metadata is not None else {},
        **doc_fields,
    )
    doc_id = doc["id"]

    chunks = chunk_text(extracted_text)
    if chunks:
        documents_helper.add_chunks(
            doc_id,
            [{"chunk_index": i, "text": t} for i, t in enumerate(chunks)],
        )
        embeddings_mod.embed_document_chunks(doc_id)

    return doc_id
