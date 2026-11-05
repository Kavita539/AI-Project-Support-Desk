import math
from collections.abc import Sequence

import pytest

from app.config import get_settings
from app.llm import EMBEDDING_DIM, EmbeddingError, FakeProvider, embed_in_batches, get_provider


def test_deterministic_dim_and_unit_norm() -> None:
    a1, a2 = FakeProvider().embed(["hello"]), FakeProvider().embed(["hello"])
    assert a1 == a2
    assert len(a1[0]) == EMBEDDING_DIM == 768
    assert math.isclose(math.sqrt(sum(x * x for x in a1[0])), 1.0, rel_tol=1e-9)


def test_different_texts_differ() -> None:
    a, b = FakeProvider().embed(["one", "two"])
    assert a != b


def test_batching_splits_and_keeps_order() -> None:
    p = FakeProvider()
    texts = [f"t{i}" for i in range(120)]
    out = embed_in_batches(p, texts)
    assert p.calls == [50, 50, 20]
    assert out == FakeProvider().embed(texts)


def test_empty_input_makes_no_call() -> None:
    p = FakeProvider()
    assert embed_in_batches(p, []) == []
    assert p.calls == []


def test_bad_batch_size() -> None:
    with pytest.raises(ValueError):
        embed_in_batches(FakeProvider(), ["a"], batch_size=0)


class _Bad:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.vectors = vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.vectors


def test_wrong_count_rejected() -> None:
    with pytest.raises(EmbeddingError) as exc:
        embed_in_batches(_Bad([[0.0] * EMBEDDING_DIM]), ["a", "b"])
    assert exc.value.retryable is False


def test_wrong_dimension_rejected() -> None:
    with pytest.raises(EmbeddingError) as exc:
        embed_in_batches(_Bad([[0.0] * 10]), ["a"])
    assert exc.value.retryable is False


def test_fail_times_then_succeeds() -> None:
    p = FakeProvider(fail_times=2)
    for _ in range(2):
        with pytest.raises(EmbeddingError) as exc:
            p.embed(["x"])
        assert exc.value.retryable is True
    assert len(p.embed(["x"])) == 1
    assert p.calls == [1, 1, 1]


def test_retryable_flag() -> None:
    p = FakeProvider(fail_times=1, retryable=False)
    with pytest.raises(EmbeddingError) as exc:
        p.embed(["x"])
    assert exc.value.retryable is False


def test_get_provider_fake_and_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        monkeypatch.setenv("LLM_PROVIDER", "fake")
        get_settings.cache_clear()
        assert isinstance(get_provider(), FakeProvider)
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="'gemini' is not available yet"):
            get_provider()
    finally:
        get_settings.cache_clear()
