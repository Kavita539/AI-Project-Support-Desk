"""GeminiProvider tests. Every call goes through httpx.MockTransport: no network."""

import json
import logging
import math
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.llm.base import (
    EMBEDDING_DIM,
    EmbeddingError,
    LLMAuthError,
    LLMBadRequest,
    LLMBlocked,
    LLMError,
    LLMMalformedOutput,
    LLMProvider,
    LLMRateLimited,
    LLMUnavailable,
)
from app.llm.gemini import GeminiProvider

KEY = "KEY123"
SECRET_BODY = "SECRET-BODY KEY123"
SCHEMA: dict[str, Any] = {"type": "object", "properties": {"a": {"type": "string"}}}

Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler) -> GeminiProvider:
    return GeminiProvider(KEY, client=httpx.Client(transport=httpx.MockTransport(handler)))


def vec(value: float = 1.0) -> list[float]:
    return [value] * EMBEDDING_DIM


def embed_ok(count: int) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [{"values": vec(2.0)}] * count})

    return handler


def gen_response(text: str, finish: str = "STOP") -> dict[str, Any]:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]}


def gen_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=gen_response('{"a": "b"}'))


def gen_returning(payload: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


# ------------------------------------------------------------------- embed


def test_embed_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return embed_ok(2)(request)

    make(handler).embed(["one", "two"], kind="query")
    (req,) = seen
    assert req.method == "POST"
    assert req.url.path.endswith("models/gemini-embedding-001:batchEmbedContents")
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)
    assert not req.url.query
    body = json.loads(req.content)
    assert len(body["requests"]) == 2
    for item, text in zip(body["requests"], ["one", "two"], strict=True):
        assert item["model"] == "models/gemini-embedding-001"
        assert item["content"] == {"parts": [{"text": text}]}
        assert item["outputDimensionality"] == 768
        assert item["taskType"] == "RETRIEVAL_QUERY"


def test_embed_document_task_type() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return embed_ok(1)(request)

    make(handler).embed(["x"])
    assert seen[0]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"


def test_embed_preserves_order_and_normalizes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"embeddings": [{"values": vec(3.0)}, {"values": [0.0] * 767 + [5.0]}]},
        )

    out = make(handler).embed(["a", "b"])
    assert len(out) == 2
    assert out[0][0] == pytest.approx(1 / math.sqrt(EMBEDDING_DIM))
    assert out[1][-1] == pytest.approx(1.0)
    assert out[1][0] == 0.0
    for v in out:
        assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0)


def test_embed_empty_makes_no_call() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return embed_ok(0)(request)

    assert make(handler).embed([]) == []
    assert calls == []


@pytest.mark.parametrize("texts", [["x"] * 101, ["x" * 8001]])
def test_embed_limits_refused_without_call(texts: list[str]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return embed_ok(len(texts))(request)

    with pytest.raises(EmbeddingError) as info:
        make(handler).embed(texts)
    assert info.value.retryable is False
    assert calls == []


def test_embed_limits_allow_boundaries() -> None:
    out = make(embed_ok(100)).embed(["x" * 8000] * 100)
    assert len(out) == 100


def embed_payload(payload: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


BAD_EMBEDDINGS: list[Any] = [
    {"embeddings": [{"values": vec()}, {"values": vec()}]},  # wrong count (2 for 1 text)
    {"embeddings": []},  # wrong count (0 for 1 text)
    {"embeddings": [{"values": [1.0] * 767}]},  # wrong dimension
    {"embeddings": [{"values": [1.0] * 769}]},
    {"embeddings": [{"values": [float("nan")] + [1.0] * 767}]},
    {"embeddings": [{"values": [True] + [1.0] * 767}]},
    {"embeddings": [{"values": ["1"] + [1.0] * 767}]},
    {"embeddings": [{"values": [0.0] * 768}]},  # zero norm
    {"embeddings": [{"values": "nope"}]},
    {"embeddings": ["nope"]},
    {"embeddings": {"values": vec()}},  # not a list
    {"other": 1},  # missing embeddings
    [1, 2, 3],  # body not an object
]


@pytest.mark.parametrize("payload", BAD_EMBEDDINGS)
def test_embed_bad_response_is_permanent(payload: Any) -> None:
    raw = json.dumps(payload).replace("NaN", "NaN")  # json.dumps emits NaN literal
    assert isinstance(raw, str)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=raw.encode(), headers={"content-type": "application/json"}
        )

    with pytest.raises(EmbeddingError) as info:
        make(handler).embed(["only"])
    assert info.value.retryable is False


# ---------------------------------------------------------------- generate


def test_generate_request_shape_with_system() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return gen_ok(request)

    result = make(handler).generate(
        "hello", SCHEMA, system="be brief", temperature=0.5, max_output_tokens=77
    )
    assert result == {"a": "b"}
    (req,) = seen
    assert req.method == "POST"
    assert req.url.path.endswith("models/gemini-3.5-flash-lite:generateContent")
    assert req.headers["x-goog-api-key"] == KEY
    assert KEY not in str(req.url)
    assert not req.url.query
    body = json.loads(req.content)
    assert body["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert body["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseSchema"] == SCHEMA
    assert cfg["temperature"] == 0.5
    assert cfg["maxOutputTokens"] == 77
    assert "tools" not in json.dumps(body)


def test_generate_without_system_omits_instruction() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return gen_ok(request)

    make(handler).generate("hi", SCHEMA)
    assert "systemInstruction" not in seen[0]
    assert "tools" not in seen[0]
    assert seen[0]["generationConfig"]["temperature"] == 0.2
    assert seen[0]["generationConfig"]["maxOutputTokens"] == 1024


def test_generate_joins_text_parts() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": '{"a":'}, {"text": ' "b"}'}]}}]}
    assert make(gen_returning(payload)).generate("p", SCHEMA) == {"a": "b"}


MALFORMED: list[dict[str, Any]] = [
    gen_response("not json"),
    gen_response("[1, 2]"),
    gen_response(""),
    gen_response("   "),
    {"candidates": []},
    {},
    {"candidates": [{"finishReason": "STOP"}]},
    {"candidates": ["x"]},
    gen_response('{"a": "b"', finish="MAX_TOKENS"),
    gen_response('{"a": "b"}', finish="max_tokens"),
]


@pytest.mark.parametrize("payload", MALFORMED)
def test_generate_malformed_is_retryable(payload: dict[str, Any]) -> None:
    with pytest.raises(LLMMalformedOutput) as info:
        make(gen_returning(payload)).generate("p", SCHEMA)
    assert info.value.retryable is True


@pytest.mark.parametrize(
    "finish",
    ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "OTHER", "IMAGE_SAFETY"],
)
@pytest.mark.parametrize("lower", [False, True])
def test_generate_blocking_finish_reason(finish: str, lower: bool) -> None:
    reason = finish.lower() if lower else finish
    with pytest.raises(LLMBlocked) as info:
        make(gen_returning(gen_response('{"a": "b"}', finish=reason))).generate("p", SCHEMA)
    assert info.value.retryable is False


def test_generate_prompt_feedback_block() -> None:
    payload = {"promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(LLMBlocked) as info:
        make(gen_returning(payload)).generate("p", SCHEMA)
    assert info.value.retryable is False


# ------------------------------------------------------------ status table

OPS = ["embed", "generate"]


def call(provider: GeminiProvider, op: str) -> object:
    if op == "embed":
        return provider.embed(["text"])
    return provider.generate("prompt", SCHEMA)


def status_handler(status: int, headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=SECRET_BODY, headers=headers)

    return handler


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(
    ("status", "exc_type", "retryable"),
    [
        (400, LLMBadRequest, False),
        (404, LLMBadRequest, False),
        (401, LLMAuthError, False),
        (402, LLMAuthError, False),
        (403, LLMAuthError, False),
        (408, LLMUnavailable, True),
        (500, LLMUnavailable, True),
        (502, LLMUnavailable, True),
        (503, LLMUnavailable, True),
        (504, LLMUnavailable, True),
        (429, LLMRateLimited, True),
    ],
)
def test_status_mapping(op: str, status: int, exc_type: type[LLMError], retryable: bool) -> None:
    with pytest.raises(LLMError) as info:
        call(make(status_handler(status)), op)
    assert type(info.value) is exc_type
    assert info.value.retryable is retryable
    assert str(info.value) == f"gemini {op}: HTTP {status}"


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("12", 12.0),
        ("1.5", 1.5),
        ("0", 0.0),
        ("abc", None),
        ("-5", None),
        ("nan", None),
        ("inf", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        (None, None),
    ],
)
def test_retry_after(op: str, header: str | None, expected: float | None) -> None:
    headers = {"Retry-After": header} if header is not None else None
    with pytest.raises(LLMRateLimited) as info:
        call(make(status_handler(429, headers)), op)
    assert info.value.retry_after == expected


def raising(exc: Exception) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


NETWORK_ERRORS: list[Exception] = [
    httpx.ConnectError("boom KEY123 SECRET-BODY"),
    httpx.ReadTimeout("boom KEY123 SECRET-BODY"),
    httpx.RemoteProtocolError("boom KEY123 SECRET-BODY"),
]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("error", NETWORK_ERRORS, ids=lambda e: type(e).__name__)
def test_network_errors_are_unavailable(op: str, error: Exception) -> None:
    with pytest.raises(LLMUnavailable) as info:
        call(make(raising(error)), op)
    assert info.value.retryable is True
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__ is True


@pytest.mark.parametrize("op", OPS)
def test_non_json_success_body_is_unavailable(op: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>SECRET-BODY KEY123</html>")

    with pytest.raises(LLMUnavailable) as info:
        call(make(handler), op)
    assert info.value.retryable is True


# ------------------------------------------------------------------ secrets


def error_cases() -> list[tuple[str, Handler]]:
    cases: list[tuple[str, Handler]] = []
    for op in OPS:
        for status in (400, 401, 403, 404, 429, 500, 503):
            cases.append((op, status_handler(status)))
        for error in NETWORK_ERRORS:
            cases.append((op, raising(error)))
        cases.append((op, status_handler(200)))
    return cases


def test_no_secret_in_any_error_or_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    cases = error_cases()
    for op, handler in cases:
        with pytest.raises(LLMError) as info:
            call(make(handler), op)
        for text in (str(info.value), repr(info.value)):
            assert "SECRET-BODY" not in text
            assert KEY not in text
    assert "SECRET-BODY" not in caplog.text
    assert KEY not in caplog.text


def test_no_secret_in_local_validation_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [], "note": SECRET_BODY})

    with pytest.raises(EmbeddingError) as info:
        make(handler).embed(["a"])
    assert "SECRET-BODY" not in str(info.value) + repr(info.value)
    assert KEY not in str(info.value) + repr(info.value)

    bad = gen_response(SECRET_BODY)
    with pytest.raises(LLMMalformedOutput) as info2:
        make(gen_returning(bad)).generate("p", SCHEMA)
    assert "SECRET-BODY" not in str(info2.value) + repr(info2.value)
    assert KEY not in caplog.text
    assert "SECRET-BODY" not in caplog.text


def test_repr_hides_key() -> None:
    provider = make(gen_ok)
    assert KEY not in repr(provider)
    assert KEY not in str(provider)


def test_empty_api_key_rejected_without_echo() -> None:
    with pytest.raises(ValueError) as info:
        GeminiProvider("")
    assert "KEY" not in str(info.value).replace("key", "")


def test_default_client_sends_key_header_and_timeouts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return gen_ok(request)

    provider = GeminiProvider(KEY, timeout_seconds=7.0)
    assert "x-goog-api-key" not in provider._client.headers
    assert KEY not in repr(dict(provider._client.headers))
    # Swap only the transport; the default client's own configuration stays under test.
    provider._client._transport = httpx.MockTransport(handler)
    provider.generate("p", SCHEMA)
    provider._client.close()
    assert seen[0].headers["x-goog-api-key"] == KEY
    assert seen[0].headers["content-type"] == "application/json"
    assert seen[0].extensions["timeout"] == {
        "connect": 5.0,
        "read": 14.0,
        "write": 10.0,
        "pool": 5.0,
    }


def test_per_request_timeouts() -> None:
    timeouts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        if request.url.path.endswith("batchEmbedContents"):
            return embed_ok(1)(request)
        return gen_ok(request)

    provider = GeminiProvider(
        KEY, timeout_seconds=7.0, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    provider.embed(["x"])
    provider.generate("p", SCHEMA)
    assert timeouts[0] == {"connect": 5.0, "read": 7.0, "write": 10.0, "pool": 5.0}
    assert timeouts[1] == {"connect": 5.0, "read": 14.0, "write": 10.0, "pool": 5.0}


def test_satisfies_llm_provider_protocol() -> None:
    provider: LLMProvider = GeminiProvider(
        KEY, client=httpx.Client(transport=httpx.MockTransport(gen_ok))
    )
    assert provider.generate("p", SCHEMA) == {"a": "b"}
