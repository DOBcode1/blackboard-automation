"""
Stage B file-text extractor.

Turns raw file bytes into plain text. Supports .txt, .md, .docx, and .pdf.
Image files (.png, .jpg, .jpeg, .webp) return (None, "needs_vision", 0) —
OCR/vision handling is provided by extract_text_via_vision (see below).

extract_text_from_file: no network calls, no project imports.
extract_text_via_vision: makes Anthropic API calls via llm_adapter.call_vision
    and requires ANTHROPIC_API_KEY in the environment.

Dependencies: python-docx, PyMuPDF. No disk writes.
"""

import base64
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


# ---------------------------------------------------------------------------
# Vision-based OCR (makes API calls)
# ---------------------------------------------------------------------------

_VISION_SYSTEM = (
    "Transcribe all visible text exactly as written. "
    "Preserve natural reading order. "
    "Output only the transcribed text with no commentary or description. "
    "If there is no readable text, output nothing."
)

_IMAGE_MEDIA_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

_VISION_IMAGE_EXTS = set(_IMAGE_MEDIA_TYPES)
_VISION_PDF_EXTS  = {".pdf"}


def extract_text_via_vision(
    data: bytes,
    filename: str,
    mime_type: str | None = None,
    max_pages: int = 25,
) -> tuple[str | None, str, int]:
    """Extract text from images and scanned PDFs using the vision model.

    Returns (extracted_text_or_None, "vision_ocr", 4).

    Makes Anthropic API calls via llm_adapter.call_vision; requires
    ANTHROPIC_API_KEY in the environment.

    Handler selection: file extension (lowercased) takes priority; mime_type
    is used only as a tiebreaker when the extension is absent or ambiguous.

    Raises ValueError for unsupported types.
    """
    from llm_adapter import call_vision  # imported here to keep module importable without API key

    ext = os.path.splitext(filename)[1].lower() if filename else ""

    # Determine which path to take; resolve via extension first, then mime_type.
    path = _vision_path_for_ext(ext)
    if path is None and mime_type:
        path = _vision_path_for_mime(mime_type)

    if path is None:
        label = ext if ext else (mime_type or "unknown")
        raise ValueError(
            f"Unsupported file type for vision extraction: {label!r}. "
            "Supported types: .png, .jpg, .jpeg, .webp, .pdf."
        )

    if path == "image":
        media_type = _IMAGE_MEDIA_TYPES.get(ext) or "image/jpeg"
        b64 = base64.standard_b64encode(data).decode("ascii")
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": "Transcribe all text in this image."},
                ],
            }
        ]
        result = call_vision(messages, system=_VISION_SYSTEM, operation="vision_ocr")
        text = result.text.strip()
        return (text or None, "vision_ocr", 4)

    # path == "pdf"
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"Could not open {filename!r} as a PDF: {exc}") from exc

    page_texts: list[str] = []
    try:
        total = doc.page_count
        if total > max_pages:
            print(
                f"[extract_text_via_vision] {filename!r} has {total} pages; "
                f"processing only the first {max_pages}."
            )
        for page_num in range(min(total, max_pages)):
            try:
                page = doc.load_page(page_num)
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                png_bytes = pix.tobytes("png")
                b64 = base64.standard_b64encode(png_bytes).decode("ascii")
                messages = [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": f"Transcribe all text on page {page_num + 1}.",
                            },
                        ],
                    }
                ]
                result = call_vision(messages, system=_VISION_SYSTEM, operation="vision_ocr")
                page_text = result.text.strip()
                if page_text:
                    page_texts.append(page_text)
            except Exception as exc:
                print(
                    f"[extract_text_via_vision] Warning: skipping page {page_num + 1} "
                    f"of {filename!r} — {exc}"
                )
    finally:
        doc.close()

    combined = "\n\n".join(page_texts)
    return (combined or None, "vision_ocr", 4)


def _vision_path_for_ext(ext: str) -> str | None:
    if ext in _VISION_IMAGE_EXTS:
        return "image"
    if ext in _VISION_PDF_EXTS:
        return "pdf"
    return None


def _vision_path_for_mime(mime_type: str) -> str | None:
    base = mime_type.split(";")[0].strip().lower()
    if base in {"image/png", "image/jpeg", "image/webp"}:
        return "image"
    if base == "application/pdf":
        return "pdf"
    return None
