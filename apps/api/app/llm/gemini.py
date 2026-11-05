"""Gemini provider over the REST API (httpx, no SDK).

Security: the API key lives only here (hard rule 10). It is sent per request in the
`x-goog-api-key` header, never in a URL. Errors carry fixed text built from the operation
name and numeric status only: never the key, URL, request body or provider response body.
`raise_for_status()` is never used because HTTPStatusError carries the URL and response.
The LLM gets no tools (hard rule 3): the `tools` field is never sent.
"""

import json
import logging
import math
from collections.abc import Sequence
from typing import Any, NoReturn

import httpx

from app.llm.base import (
    EMBEDDING_DIM,
    EmbeddingError,
    EmbedKind,
    LLMAuthError,
    LLMBadRequest,
    LLMBlocked,
    LLMMalformedOutput,
    LLMRateLimited,
    LLMUnavailable,
)

logger = logging.getLogger(__name__)

# UNVERIFIED: these exact REST field names are written from memory of the Gemini API and
# have not been checked against the live API. Confirm them at the first live check.
_DIM_FIELD = "outputDimensionality"
_TASK_FIELD = "taskType"
_SCHEMA_FIELD = "responseSchema"
_TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
_TASK_QUERY = "RETRIEVAL_QUERY"
_API_KEY_HEADER = "x-goog-api-key"

MAX_EMBED_BATCH = 100
MAX_EMBED_CHARS = 8000

_BLOCKING_FINISH = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "OTHER", "IMAGE_SAFETY"}
)


class GeminiProvider:
    def __init__(
        self,
        api_key: str,
        *,
        embed_model: str = "gemini-embedding-001",
        generation_model: str = "gemini-3.5-flash-lite",
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Gemini API key must not be empty")
        self._api_key = api_key
        self._embed_model = embed_model
        self._generation_model = generation_model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        if client is None:
            # The key is sent per request in _post, never stored on the client.
            client = httpx.Client()
        self._client = client

    def __repr__(self) -> str:
        return (
            f"GeminiProvider(embed_model={self._embed_model!r}, "
            f"generation_model={self._generation_model!r})"
        )

    # ------------------------------------------------------------------ embed

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]:
        if not texts:
            return []
        if len(texts) > MAX_EMBED_BATCH:
            raise EmbeddingError(
                f"gemini embed: batch larger than {MAX_EMBED_BATCH}", retryable=False
            )
        if any(len(t) > MAX_EMBED_CHARS for t in texts):
            raise EmbeddingError(
                f"gemini embed: text longer than {MAX_EMBED_CHARS} characters", retryable=False
            )
        task = _TASK_QUERY if kind == "query" else _TASK_DOCUMENT
        model_ref = f"models/{self._embed_model}"
        body: dict[str, Any] = {
            "requests": [
                {
                    "model": model_ref,
                    "content": {"parts": [{"text": t}]},
                    _TASK_FIELD: task,
                    _DIM_FIELD: EMBEDDING_DIM,
                }
                for t in texts
            ]
        }
        url = f"{self._base_url}/{model_ref}:batchEmbedContents"
        data = self._post("embed", url, body, read_timeout=self._timeout_seconds)
        return _parse_embeddings(data, len(texts))

    # --------------------------------------------------------------- generate

    def generate(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 1024,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                _SCHEMA_FIELD: schema,
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if system is not None:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self._base_url}/models/{self._generation_model}:generateContent"
        data = self._post("generate", url, body, read_timeout=2 * self._timeout_seconds)
        return _parse_generation(data)

    # ------------------------------------------------------------------- http

    def _post(
        self, op: str, url: str, body: dict[str, Any], *, read_timeout: float
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=10.0, pool=5.0)
        headers = {_API_KEY_HEADER: self._api_key, "content-type": "application/json"}
        try:
            response = self._client.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            logger.warning("gemini %s: timeout", op)
            raise LLMUnavailable(f"gemini {op}: timeout", retryable=True) from None
        except httpx.HTTPError:
            logger.warning("gemini %s: network error", op)
            raise LLMUnavailable(f"gemini {op}: network error", retryable=True) from None

        status = response.status_code
        if 200 <= status < 300:
            try:
                parsed = response.json()
            except ValueError:
                logger.warning("gemini %s: non-JSON response", op)
                raise LLMUnavailable(f"gemini {op}: invalid response", retryable=True) from None
            if not isinstance(parsed, dict):
                return {}
            return parsed

        logger.warning("gemini %s: HTTP %d", op, status)
        _raise_for_status(op, status, response.headers.get("retry-after"))


def _raise_for_status(op: str, status: int, retry_after_header: str | None) -> NoReturn:
    message = f"gemini {op}: HTTP {status}"
    if status == 429:
        raise LLMRateLimited(
            message, retryable=True, retry_after=_parse_retry_after(retry_after_header)
        ) from None
    if status == 408 or status >= 500:
        raise LLMUnavailable(message, retryable=True) from None
    if status in (401, 402, 403):
        raise LLMAuthError(message, retryable=False) from None
    raise LLMBadRequest(message, retryable=False) from None


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


# ------------------------------------------------------------------- parsing


def _bad_embedding(reason: str) -> EmbeddingError:
    return EmbeddingError(f"gemini embed: {reason}", retryable=False)


def _parse_embeddings(data: dict[str, Any], expected: int) -> list[list[float]]:
    items = data.get("embeddings")
    if not isinstance(items, list):
        raise _bad_embedding("response has no embeddings list")
    if len(items) != expected:
        raise _bad_embedding(f"expected {expected} embeddings, got {len(items)}")
    vectors: list[list[float]] = []
    for item in items:
        values = item.get("values") if isinstance(item, dict) else None
        if not isinstance(values, list) or len(values) != EMBEDDING_DIM:
            raise _bad_embedding(f"vector is not a list of {EMBEDDING_DIM} numbers")
        floats: list[float] = []
        for v in values:
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise _bad_embedding("vector contains a non-number")
            f = float(v)
            if not math.isfinite(f):
                raise _bad_embedding("vector contains a non-finite number")
            floats.append(f)
        norm = math.sqrt(sum(x * x for x in floats))
        if not math.isfinite(norm) or norm == 0.0:
            raise _bad_embedding("vector has zero or invalid norm")
        vectors.append([x / norm for x in floats])
    return vectors


def _parse_generation(data: dict[str, Any]) -> dict[str, Any]:
    feedback = data.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise LLMBlocked("gemini generate: prompt blocked", retryable=False)

    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise LLMMalformedOutput("gemini generate: no candidates", retryable=True)
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise LLMMalformedOutput("gemini generate: bad candidate", retryable=True)

    finish = candidate.get("finishReason")
    if isinstance(finish, str):
        reason = finish.upper()
        if reason in _BLOCKING_FINISH:
            raise LLMBlocked("gemini generate: response blocked", retryable=False)
        if reason == "MAX_TOKENS":
            raise LLMMalformedOutput("gemini generate: truncated", retryable=True)

    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    text = ""
    if isinstance(parts, list):
        text = "".join(
            p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    if not text.strip():
        raise LLMMalformedOutput("gemini generate: empty output", retryable=True)
    try:
        result = json.loads(text)
    except ValueError:
        raise LLMMalformedOutput("gemini generate: output is not JSON", retryable=True) from None
    if not isinstance(result, dict):
        raise LLMMalformedOutput("gemini generate: output is not an object", retryable=True)
    return result
