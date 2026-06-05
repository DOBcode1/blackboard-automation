"""
Stage B file-text extractor.

Turns raw file bytes into plain text. Supports .txt, .md, .docx, and .pdf.
Image files (.png, .jpg, .jpeg, .webp) return (None, "needs_vision", 0) —
OCR/vision handling is added in a later step.

Dependencies: python-docx, PyMuPDF. No project modules are imported.
No disk writes, no network calls.
"""

import io
import os


def extract_text_from_file(
    data: bytes,
    filename: str,
    mime_type: str | None = None,
) -> tuple[str | None, str, int]:
    """Extract plain text from raw file bytes.

    Returns (extracted_text_or_None, extraction_method, extraction_confidence).

    Handler selection: file extension (lowercased) takes priority; mime_type
    is used only as a tiebreaker when the extension is absent or ambiguous.

    Raises ValueError for unsupported types.
    Raises an informative exception if the file is corrupt or unreadable.
    """
    ext = os.path.splitext(filename)[1].lower() if filename else ""

    # Resolve handler from extension, falling back to mime_type.
    handler = _handler_for_ext(ext)
    if handler is None and mime_type:
        handler = _handler_for_mime(mime_type)

    if handler is None:
        label = ext if ext else (mime_type or "unknown")
        raise ValueError(
            f"Unsupported file type: {label!r}. "
            "Supported types: .txt, .md, .docx, .pdf, .png, .jpg, .jpeg, .webp."
        )

    return handler(data, filename)


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

def _handler_for_ext(ext: str):
    return {
        ".txt":  _handle_plain_text,
        ".md":   _handle_plain_text,
        ".docx": _handle_docx,
        ".pdf":  _handle_pdf,
        ".png":  _handle_image,
        ".jpg":  _handle_image,
        ".jpeg": _handle_image,
        ".webp": _handle_image,
    }.get(ext)


def _handler_for_mime(mime_type: str):
    # Normalise: strip parameters (e.g. "text/plain; charset=utf-8" → "text/plain")
    base = mime_type.split(";")[0].strip().lower()
    return {
        "text/plain":    _handle_plain_text,
        "text/markdown": _handle_plain_text,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _handle_docx,
        "application/pdf": _handle_pdf,
        "image/png":  _handle_image,
        "image/jpeg": _handle_image,
        "image/webp": _handle_image,
    }.get(base)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_plain_text(data: bytes, filename: str) -> tuple[str | None, str, int]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    return (text.strip() or None, "native_text", 5)


def _handle_docx(data: bytes, filename: str) -> tuple[str | None, str, int]:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise ImportError(
            "python-docx is required to read .docx files. "
            "Install it with: pip install python-docx"
        ) from exc

    try:
        doc = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"Could not open {filename!r} as a .docx file: {exc}") from exc

    text = "\n".join(p.text for p in doc.paragraphs)
    return (text.strip() or None, "native_text", 5)


def _handle_pdf(data: bytes, filename: str) -> tuple[str | None, str, int]:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise ImportError(
            "PyMuPDF is required to read .pdf files. "
            "Install it with: pip install pymupdf"
        ) from exc

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open {filename!r} as a PDF: {exc}") from exc

    pages: list[str] = []
    try:
        for page in doc:
            pages.append(page.get_text())
    finally:
        doc.close()

    text = "\n".join(pages)
    non_ws = sum(1 for c in text if not c.isspace())
    if non_ws > 20:
        return (text.strip(), "native_text", 5)
    return (None, "needs_vision", 0)


def _handle_image(data: bytes, filename: str) -> tuple[str | None, str, int]:
    return (None, "needs_vision", 0)
