"""Turn an uploaded file's bytes into text pages. The content is untrusted data: it is only
decoded and measured here, never executed or interpreted."""

import re
import time
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader

_PDF_UNREADABLE = (
    "This PDF could not be read. It may be damaged or protected. Please upload a valid copy."
)
_NO_TEXT = (
    "No text could be found in this file. It may be a scan or images only. "
    "Upload a text-based version, or run OCR on it first."
)
_NOT_UTF8 = "This file is not valid UTF-8 text."
_UNSUPPORTED = "This document type cannot be processed yet."
_FAQ_MALFORMED = "This FAQ entry is not in the expected format. Please re-create it."

FAQ_QUESTION_MAX = 300
FAQ_ANSWER_MAX = 2400

_HEADING = re.compile(r"^#{1,6}\s+(\S.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_MANY_NEWLINES = re.compile(r"\n{3,}")


class PermanentIngestError(Exception):
    """The file can never be ingested; retrying will not help. user_message is safe to show."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True)
class ParsedPage:
    page: int | None
    text: str
    section: str | None


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    # Lone surrogates (seen in odd PDFs) cannot be encoded to UTF-8 and would fail the insert.
    text = text.encode("utf-8", "ignore").decode("utf-8")
    return _MANY_NEWLINES.sub("\n\n", text).strip()


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")  # also drops a leading BOM
    except UnicodeDecodeError:
        raise PermanentIngestError(_NOT_UTF8) from None


def _parse_pdf(
    data: bytes, max_pages: int, max_chars: int, deadline_seconds: float
) -> list[ParsedPage]:
    start = time.monotonic()
    pages: list[ParsedPage] = []
    total = 0
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise PermanentIngestError(_PDF_UNREADABLE)
        count = len(reader.pages)
        if count > max_pages:
            raise PermanentIngestError(f"This PDF has {count} pages. The limit is {max_pages}.")
        for number in range(count):
            if time.monotonic() - start >= deadline_seconds:
                raise PermanentIngestError(
                    "This PDF took too long to read. Try splitting it into smaller files."
                )
            text = _clean(reader.pages[number].extract_text() or "")
            total += len(text)
            if total > max_chars:
                raise PermanentIngestError(
                    "This PDF contains too much text. Try splitting it into smaller files."
                )
            if text:
                pages.append(ParsedPage(page=number + 1, text=text, section=None))
    except PermanentIngestError:
        raise
    except Exception:  # untrusted file: any parser failure means "cannot be read"
        raise PermanentIngestError(_PDF_UNREADABLE) from None
    if not pages:
        raise PermanentIngestError(_NO_TEXT)
    return pages


def _parse_md(data: bytes) -> list[ParsedPage]:
    text = _clean(_decode(data))
    sections: list[ParsedPage] = []
    section: str | None = None
    buffer: list[str] = []
    in_fence = False

    def flush() -> None:
        body = _clean("\n".join(buffer))
        if body:
            sections.append(ParsedPage(page=None, text=body, section=section))
        buffer.clear()

    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            flush()
            section = heading.group(1)
        buffer.append(line)
    flush()
    if not sections:
        raise PermanentIngestError(_NO_TEXT)
    return sections


def _parse_txt(data: bytes) -> list[ParsedPage]:
    text = _clean(_decode(data))
    if not text:
        raise PermanentIngestError(_NO_TEXT)
    return [ParsedPage(page=None, text=text, section=None)]


def format_faq(question: str, answer: str) -> str:
    """The stored FAQ file format: a single-line question, then the answer."""
    question = " ".join(question.split())
    return f"Q: {question}\nA: {answer.strip()}\n"


def _parse_faq(data: bytes) -> list[ParsedPage]:
    text = _clean(_decode(data))
    head, sep, answer = text.partition("\nA: ")
    question = head[3:].strip() if head.startswith("Q: ") else ""
    answer = answer.strip()
    if not sep or not question or not answer:
        raise PermanentIngestError(_FAQ_MALFORMED)
    return [ParsedPage(page=None, text=f"Q: {question}\nA: {answer}", section=question[:200])]


def parse_document(
    kind: str,
    data: bytes,
    *,
    max_pages: int = 100,
    max_chars: int = 2_000_000,
    deadline_seconds: float = 60.0,
) -> list[ParsedPage]:
    if kind == "pdf":
        return _parse_pdf(data, max_pages, max_chars, deadline_seconds)
    if kind == "md":
        return _parse_md(data)
    if kind == "txt":
        return _parse_txt(data)
    if kind == "faq":
        return _parse_faq(data)
    raise PermanentIngestError(_UNSUPPORTED)
