"""Provider interface for the LLM: embeddings, schema-validated JSON generation, and errors."""

from collections.abc import Sequence
from typing import Any, Literal, Protocol

EMBEDDING_DIM = 768

# Document chunks and user questions are embedded differently by some vendors.
EmbedKind = Literal["document", "query"]


class LLMError(Exception):
    """Base for every provider failure. `retryable` says whether trying again later could work.

    Messages are fixed text: never the key, URL, request body or provider response body.
    """

    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class EmbeddingError(LLMError):
    """Embedding failed. The message is technical (logs / last_error), never shown to customers."""


class LLMRateLimited(LLMError):
    """The provider returned 429 (quota or rate limit). Retryable."""


class LLMUnavailable(LLMError):
    """5xx, network failure or timeout. Retryable."""


class LLMBlocked(LLMError):
    """The provider refused on safety or policy grounds. Permanent."""


class LLMMalformedOutput(LLMError):
    """Bad JSON, schema mismatch, empty or truncated output. Retryable."""


class LLMAuthError(LLMError):
    """401, 402 or 403: bad key, billing or permission. Permanent."""


class LLMBadRequest(LLMError):
    """Any other 4xx: the request itself is wrong. Permanent."""


class Embedder(Protocol):
    """The embedding half of an LLM vendor."""

    def embed(self, texts: Sequence[str], *, kind: EmbedKind = "document") -> list[list[float]]:
        """Return one EMBEDDING_DIM-float vector per input text, in the same order."""
        ...


class LLMProvider(Embedder, Protocol):
    """What the app needs from an LLM vendor.

    The LLM never has tools or write access (hard rule 3): `generate` returns JSON
    that matches `schema`, and the server validates and stores it.
    """

    def generate(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Return a JSON object matching `schema`. Raises an LLMError subclass on failure."""
        ...


def embed_in_batches(
    provider: Embedder,
    texts: Sequence[str],
    batch_size: int = 50,
    *,
    kind: EmbedKind = "document",
) -> list[list[float]]:
    """Embed texts in order, batch_size at a time, validating each provider response."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        result = provider.embed(batch, kind=kind)
        if len(result) != len(batch):
            raise EmbeddingError(
                f"provider returned {len(result)} vectors for {len(batch)} texts",
                retryable=False,
            )
        for vec in result:
            if len(vec) != EMBEDDING_DIM:
                raise EmbeddingError(
                    f"provider returned a vector of {len(vec)} floats, expected {EMBEDDING_DIM}",
                    retryable=False,
                )
        vectors.extend(result)
    return vectors
