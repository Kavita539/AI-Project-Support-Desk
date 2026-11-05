from io import BytesIO

import pytest
from pypdf import PdfWriter

from app.services.parsing import PermanentIngestError, _clean, format_faq, parse_document

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


@pytest.mark.parametrize("kind", ["docx", ""])
def test_unsupported_kind(kind: str) -> None:
    assert err(kind, b"x") == "This document type cannot be processed yet."


FAQ_BAD = "This FAQ entry is not in the expected format. Please re-create it."


def test_faq_round_trip() -> None:
    raw = format_faq("  How do I reset?  ", "  Click reset.\nThen wait. ").encode()
    pages = parse_document("faq", raw)
    assert len(pages) == 1
    assert pages[0].page is None
    assert pages[0].text == "Q: How do I reset?\nA: Click reset.\nThen wait."
    assert pages[0].section == "How do I reset?"


def test_faq_newline_in_question_collapsed() -> None:
    formatted = format_faq("line one\n\n  line   two\r\nthree", "ans")
    assert formatted == "Q: line one line two three\nA: ans\n"
    assert parse_document("faq", formatted.encode())[0].section == "line one line two three"


def test_faq_question_containing_a_marker_still_parses() -> None:
    q = "What does A: mean? and \\nA: odd"
    pages = parse_document("faq", format_faq(q, "It means answer.").encode())
    assert pages[0].text == f"Q: {q}\nA: It means answer."


def test_faq_answer_containing_a_marker_keeps_all_of_it() -> None:
    pages = parse_document("faq", format_faq("Q1?", "first\nA: second").encode())
    assert pages[0].text == "Q: Q1?\nA: first\nA: second"


def test_faq_section_truncated_to_200() -> None:
    pages = parse_document("faq", format_faq("x" * 300, "ans").encode())
    assert pages[0].section == "x" * 200


@pytest.mark.parametrize(
    "data",
    [
        b"Q: only a question\n",
        b"Q: question\nA: \n",
        b"Q: question\nA:no space\n",
        b"Q: \nA: answer\n",
        b"A: answer\nQ: question\n",
        b"just some text",
        b"",
    ],
)
def test_faq_malformed_gets_fixed_message(data: bytes) -> None:
    assert err("faq", data) == FAQ_BAD


def test_faq_invalid_utf8_is_readable_and_leaks_nothing() -> None:
    message = err("faq", b"Q: q\nA: \xff\xfe")
    assert message == "This file is not valid UTF-8 text."
    for banned in ("codec", "byte", "position", "Traceback"):
        assert banned not in message


def test_markdown_bom_is_dropped_so_first_heading_survives() -> None:
    pages = parse_document("md", "﻿# Title\nbody".encode())
    assert pages[0].section == "Title"


def test_lone_surrogates_are_removed() -> None:
    # PDF text extraction can produce them; they cannot be encoded to UTF-8 for the insert.
    cleaned = _clean("ok\ud800 text")
    assert cleaned == "ok text"
    cleaned.encode("utf-8")
