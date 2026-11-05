"""Split parsed pages into overlapping chunks. Deterministic and always terminates."""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.parsing import ParsedPage

_SEP = "\n\n"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class TextChunk:
    position: int
    content: str
    page: int | None
    section: str | None


def _hard_cut(text: str, limit: int) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def _split_long(paragraph: str, limit: int) -> list[str]:
    """Break one oversized paragraph into pieces of at most `limit` chars: sentences first,
    then a hard cut for any sentence that is still too long."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        if not sentence:
            continue
        if len(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(_hard_cut(sentence, limit))
        elif current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        pieces.append(current)
    return pieces


def _tail(content: str, size: int) -> str:
    """About the last `size` chars of content, starting at a word boundary when possible."""
    if size <= 0:
        return ""
    if len(content) <= size:
        return content
    tail = content[-size:]
    if not content[-size - 1].isspace():
        cut = next((i for i, ch in enumerate(tail) if ch.isspace()), -1)
        if cut != -1:
            tail = tail[cut:]
    return tail.strip()


def _chunk_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    piece_limit = max_chars - overlap_chars  # >= 1, leaves room for the overlap prefix
    pieces: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= piece_limit:
            pieces.append(paragraph)
        else:
            pieces.extend(p.strip() for p in _split_long(paragraph, piece_limit) if p.strip())

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) + len(_SEP) + len(piece) <= max_chars:
            current = f"{current}{_SEP}{piece}"
        else:
            chunks.append(current)
            budget = min(overlap_chars, max_chars - len(_SEP) - len(piece))
            tail = _tail(current, budget)
            current = f"{tail}{_SEP}{piece}" if tail else piece
    if current:
        chunks.append(current)
    return chunks


def chunk_pages(
    pages: Sequence[ParsedPage], *, max_chars: int = 2800, overlap_chars: int = 400
) -> list[TextChunk]:
    if max_chars < 1 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be >= 0 and smaller than max_chars")
    result: list[TextChunk] = []
    for page in pages:
        for content in _chunk_text(page.text, max_chars, overlap_chars):
            content = content.strip()
            if content:
                result.append(TextChunk(len(result), content, page.page, page.section))
    return result
