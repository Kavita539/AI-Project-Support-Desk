import pytest

from app.services.chunking import chunk_pages
from app.services.parsing import ParsedPage


def page(text: str, n: int | None = None, section: str | None = None) -> ParsedPage:
    return ParsedPage(page=n, text=text, section=section)


def test_empty_list() -> None:
    assert chunk_pages([]) == []


def test_small_text_single_chunk() -> None:
    chunks = chunk_pages([page("Hello world.", 1, "Intro")])
    assert len(chunks) == 1
    assert (chunks[0].position, chunks[0].content, chunks[0].page, chunks[0].section) == (
        0,
        "Hello world.",
        1,
        "Intro",
    )


def test_whitespace_only_page_yields_nothing() -> None:
    assert chunk_pages([page("  \n\n  ")]) == []


def test_size_bound_and_positions() -> None:
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(40))
    chunks = chunk_pages([page(text)], max_chars=500, overlap_chars=100)
    assert len(chunks) > 3
    assert [c.position for c in chunks] == list(range(len(chunks)))
    assert all(0 < len(c.content) <= 500 for c in chunks)
    assert all(c.content.strip() == c.content and c.content for c in chunks)


def test_overlap_between_consecutive_chunks() -> None:
    text = "\n\n".join(f"Para{i} " + "x" * 80 for i in range(30))
    chunks = chunk_pages([page(text)], max_chars=400, overlap_chars=150)
    assert len(chunks) > 2
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        first_para = nxt.content.split("\n\n")[0]
        assert first_para and first_para in prev.content


def test_long_single_paragraph_no_spaces() -> None:
    chunks = chunk_pages([page("a" * 10_000)], max_chars=1000, overlap_chars=200)
    assert len(chunks) >= 10
    assert all(len(c.content) <= 1000 for c in chunks)
    assert sum(len(c.content) for c in chunks) >= 10_000


def test_long_paragraph_splits_on_sentences() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_pages([page(text)], max_chars=300, overlap_chars=50)
    assert all(len(c.content) <= 300 for c in chunks)
    assert all(c.content.rstrip().endswith(".") for c in chunks)


def test_unicode() -> None:
    text = "\n\n".join("héllo wörld 日本語のテキスト 🙂 " * 20 for _ in range(10))
    chunks = chunk_pages([page(text)], max_chars=300, overlap_chars=50)
    assert chunks
    assert all(len(c.content) <= 300 for c in chunks)
    assert "日本語" in chunks[0].content


def test_deterministic() -> None:
    pages = [page("alpha beta. " * 400, 1), page("gamma delta. " * 400, 2)]
    assert chunk_pages(pages, max_chars=500, overlap_chars=80) == chunk_pages(
        pages, max_chars=500, overlap_chars=80
    )


def test_page_and_section_carried() -> None:
    pages = [page("one " * 300, 1, "A"), page("two " * 300, 2, "B"), page("three", None, None)]
    chunks = chunk_pages(pages, max_chars=400, overlap_chars=50)
    assert [c.position for c in chunks] == list(range(len(chunks)))
    assert chunks[0].page == 1 and chunks[0].section == "A"
    assert any(c.page == 2 and c.section == "B" for c in chunks)
    assert chunks[-1].page is None and chunks[-1].content == "three"
    assert all("two" not in c.content for c in chunks if c.page == 1)


@pytest.mark.parametrize("overlap", [400, 500, -1])
def test_bad_overlap_raises(overlap: int) -> None:
    with pytest.raises(ValueError):
        chunk_pages([page("x")], max_chars=400, overlap_chars=overlap)


def test_tiny_limits_terminate() -> None:
    chunks = chunk_pages([page("abcdefghij " * 20)], max_chars=5, overlap_chars=4)
    assert chunks
    assert all(len(c.content) <= 5 for c in chunks)
    chunks = chunk_pages([page("abcdef")], max_chars=1, overlap_chars=0)
    assert [c.content for c in chunks] == list("abcdef")
