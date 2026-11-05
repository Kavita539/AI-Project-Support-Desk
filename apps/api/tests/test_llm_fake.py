import math
from collections.abc import Sequence
from typing import Any

import pytest

from app.llm import EMBEDDING_DIM, EmbeddingError, FakeProvider, embed_in_batches
from app.llm.base import (
    Embedder,
    EmbedKind,
    LLMAuthError,
    LLMBadRequest,
    LLMBlocked,
    LLMError,
    LLMMalformedOutput,
    LLMProvider,
    LLMRateLimited,
    LLMUnavailable,
)
from app.llm.fake import GenerateCall


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

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]:
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


def test_error_hierarchy() -> None:
    for cls in (
        EmbeddingError,
        LLMRateLimited,
        LLMUnavailable,
        LLMBlocked,
        LLMMalformedOutput,
        LLMAuthError,
        LLMBadRequest,
    ):
        assert issubclass(cls, LLMError)
        assert cls("x", retryable=True).retryable is True


def test_error_retry_after() -> None:
    err = EmbeddingError("x", retryable=False)
    assert err.retryable is False
    assert err.retry_after is None
    assert str(err) == "x"
    assert LLMRateLimited("slow down", retryable=True, retry_after=2.5).retry_after == 2.5


def test_fake_records_embed_kinds() -> None:
    p = FakeProvider(fail_times=1)
    with pytest.raises(EmbeddingError):
        p.embed(["a"], kind="query")
    p.embed(["a"])
    p.embed(["a"], kind="document")
    assert p.kinds == ["query", "document", "document"]


def test_embed_in_batches_passes_kind() -> None:
    p = FakeProvider()
    embed_in_batches(p, [f"t{i}" for i in range(60)], kind="query")
    assert p.kinds == ["query", "query"]
    p2 = FakeProvider()
    embed_in_batches(p2, ["a"])
    assert p2.kinds == ["document"]


def test_vectors_same_for_document_and_query() -> None:
    p = FakeProvider()
    assert p.embed(["same"], kind="document") == p.embed(["same"], kind="query")


def test_generate_returns_queued_in_order() -> None:
    p = FakeProvider()
    p.queue_generate({"n": 1}, {"n": 2})
    assert p.generate("p", {}) == {"n": 1}
    assert p.generate("p", {}) == {"n": 2}


def test_generate_returns_deep_copy() -> None:
    original: dict[str, Any] = {"a": {"b": [1]}}
    p = FakeProvider()
    p.queue_generate(original)
    first = p.generate("p", {})
    first["a"]["b"].append(2)
    assert original == {"a": {"b": [1]}}
    p.queue_generate(original)
    assert p.generate("p", {}) == {"a": {"b": [1]}}


def test_generate_raises_queued_error() -> None:
    p = FakeProvider()
    p.queue_generate(LLMBlocked("blocked", retryable=False), LLMUnavailable("down", retryable=True))
    with pytest.raises(LLMBlocked) as blocked:
        p.generate("p", {})
    assert blocked.value.retryable is False
    with pytest.raises(LLMUnavailable) as down:
        p.generate("p", {})
    assert down.value.retryable is True


def test_generate_empty_queue_asserts() -> None:
    with pytest.raises(AssertionError, match="no scripted response"):
        FakeProvider().generate("p", {})


def test_generate_calls_recorded() -> None:
    p = FakeProvider(generate_responses=[{"ok": True}])
    schema = {"type": "object"}
    p.generate("hello", schema, system="sys", temperature=0.5, max_output_tokens=64)
    assert p.generate_calls == [GenerateCall("hello", schema, "sys", 0.5, 64)]


def test_generate_calls_recorded_even_when_raising() -> None:
    p = FakeProvider()
    with pytest.raises(AssertionError):
        p.generate("p", {"x": 1})
    assert p.generate_calls[0].system is None
    assert p.generate_calls[0].temperature == 0.2
    assert p.generate_calls[0].max_output_tokens == 1024


def test_constructor_generate_responses() -> None:
    err = LLMMalformedOutput("bad json", retryable=True)
    p = FakeProvider(0, True, [{"a": 1}, err])
    assert p.generate("p", {}) == {"a": 1}
    with pytest.raises(LLMMalformedOutput):
        p.generate("p", {})


def test_fake_satisfies_protocols() -> None:
    provider: LLMProvider = FakeProvider()
    embedder: Embedder = FakeProvider()
    assert len(provider.embed(["x"])[0]) == EMBEDDING_DIM
    assert len(embedder.embed(["x"], kind="query")) == 1


