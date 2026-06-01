"""Pure text-splitting logic for chunking documents into overlapping segments."""

CHUNK_SIZE = 1500     # target maximum chunk length in characters
CHUNK_OVERLAP = 200   # characters of overlap carried between consecutive chunks
SEPARATORS = ["\n\n", "\n", ". ", " ", ""]  # tried in order, coarsest to finest


def _split_recursive(text: str, separators: list[str], chunk_size: int) -> list[str]:
    """Recursively split text into segments each no longer than chunk_size.

    Tries separators from coarsest to finest. If a separator is found, splits
    on it (re-attaching it to preserve all characters) and recurses on oversized
    pieces with the remaining finer separators. Falls back to hard-slicing when
    no separator applies.
    """
    if len(text) <= chunk_size:
        return [text]

    # Find the first separator that actually appears in the text.
    chosen_sep = ""
    remaining_seps: list[str] = []
    for i, sep in enumerate(separators):
        if sep == "" or sep in text:
            chosen_sep = sep
            remaining_seps = separators[i + 1:]
            break

    # Hard-split when no meaningful separator is available.
    if chosen_sep == "":
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

    # Split on the chosen separator, re-attaching it to preserve all characters.
    raw_pieces = text.split(chosen_sep)
    pieces: list[str] = []
    for j, piece in enumerate(raw_pieces):
        if j < len(raw_pieces) - 1:
            piece = piece + chosen_sep
        if piece:
            pieces.append(piece)

    # Recurse on any piece that is still too long.
    segments: list[str] = []
    for piece in pieces:
        if len(piece) <= chunk_size:
            segments.append(piece)
        else:
            segments.extend(_split_recursive(piece, remaining_seps, chunk_size))

    return segments


def _merge_with_overlap(segments: list[str], chunk_size: int, overlap: int) -> list[str]:
    """Greedily combine segments into chunks, seeding each new chunk with
    the trailing `overlap` characters of the previous one.
    """
    chunks: list[str] = []
    buffer = ""

    for seg in segments:
        if buffer and len(buffer) + len(seg) > chunk_size:
            chunks.append(buffer)
            # Seed next buffer with trailing overlap from the emitted chunk.
            buffer = buffer[-overlap:] if overlap else ""
        buffer += seg

    if buffer:
        chunks.append(buffer)

    return chunks


def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split *text* into overlapping chunks of at most *chunk_size* characters.

    Uses a recursive character splitter that tries progressively finer
    separators before falling back to hard character slices. Consecutive chunks
    share up to *overlap* characters so context is not lost at boundaries.

    Returns an empty list when *text* is None or contains only whitespace.
    """
    if not text or not text.strip():
        return []

    segments = _split_recursive(text, SEPARATORS, chunk_size)
    chunks = _merge_with_overlap(segments, chunk_size, overlap)
    return [c.strip() for c in chunks if c.strip()]
