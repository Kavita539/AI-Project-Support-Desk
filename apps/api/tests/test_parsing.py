from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.services.parsing import PermanentIngestError, _clean, parse_document

CORRUPT = "This PDF could not be read. It may be damaged or protected. Please upload a valid copy."
NO_TEXT = (
    "No text could be found in this file. It may be a scan or images only. "
    "Upload a text-based version, or run OCR on it first."
)


def text_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF; an empty string gives a page with no text."""
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content_id = 5 + 2 * i
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        stream = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode() if text else b""
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def err(kind: str, data: bytes, **kw: float) -> str:
    with pytest.raises(PermanentIngestError) as exc:
        parse_document(kind, data, **kw)  # type: ignore[arg-type]
    return exc.value.user_message


def test_text_pdf_page_numbers_and_blank_skipped() -> None:
    pages = parse_document("pdf", text_pdf(["Hello one", "", "Hello three"]))
    assert [(p.page, p.text, p.section) for p in pages] == [
        (1, "Hello one", None),
        (3, "Hello three", None),
    ]


def test_blank_pdf_is_scanned_error() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = BytesIO()
    writer.write(buf)
    assert err("pdf", buf.getvalue()) == NO_TEXT


def test_garbage_with_pdf_header() -> None:
    assert err("pdf", b"%PDF-1.7\n" + b"\x01garbage" * 50) == CORRUPT


def test_truncated_pdf() -> None:
    data = text_pdf(["Hello"])
    assert err("pdf", data[: len(data) // 2]) == CORRUPT


def test_encrypted_pdf() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    buf = BytesIO()
    writer.write(buf)
    assert err("pdf", buf.getvalue()) == CORRUPT


def test_too_many_pages() -> None:
    message = err("pdf", text_pdf(["a", "b", "c"]), max_pages=2)
    assert "3 pages" in message and "2" in message


def test_deadline_exceeded() -> None:
    assert "too long" in err("pdf", text_pdf(["a", "b"]), deadline_seconds=0)


def test_too_much_text() -> None:
    assert "too much text" in err("pdf", text_pdf(["x" * 50, "y" * 50]), max_chars=60)


def test_md_sections() -> None:
    md = b"intro line\n\n# Title\nbody a\n\n## Sub ##\nbody b\n```\n# not heading\n```\n"
    pages = parse_document("md", md)
    assert [(p.page, p.section) for p in pages] == [(None, None), (None, "Title"), (None, "Sub")]
    assert pages[0].text == "intro line"
    assert pages[1].text.startswith("# Title") and "body a" in pages[1].text
    assert "# not heading" in pages[2].text


def test_md_empty() -> None:
    assert err("md", b"  \n\n ") == NO_TEXT


def test_txt_single_page_and_normalisation() -> None:
    pages = parse_document("txt", b"a\r\nb\r\r\r\n\n\nc\x00d")
    assert len(pages) == 1
    assert (pages[0].page, pages[0].section) == (None, None)
    assert pages[0].text == "a\nb\n\ncd"


def test_nul_stripped_in_md_and_pdf() -> None:
    assert "\x00" not in parse_document("md", b"# T\nab\x00cd")[0].text
    assert "\x00" not in parse_document("pdf", text_pdf(["Hello"]))[0].text


@pytest.mark.parametrize("kind", ["txt", "md"])
def test_non_utf8(kind: str) -> None:
    assert err(kind, b"\xff\xfe\xfa bad") == "This file is not valid UTF-8 text."


def test_empty_txt() -> None:
    assert err("txt", b"\n \x00 ") == NO_TEXT


@pytest.mark.parametrize("kind", ["faq", "docx", ""])
def test_unsupported_kind(kind: str) -> None:
    assert err(kind, b"x") == "This document type cannot be processed yet."


def test_markdown_bom_is_dropped_so_first_heading_survives() -> None:
    pages = parse_document("md", "﻿# Title\nbody".encode())
    assert pages[0].section == "Title"


def test_lone_surrogates_are_removed() -> None:
    # PDF text extraction can produce them; they cannot be encoded to UTF-8 for the insert.
    cleaned = _clean("ok\ud800 text")
    assert cleaned == "ok text"
    cleaned.encode("utf-8")
