"""Provider interface for the LLM (embedding half; `generate` arrives in step 9)."""

from collections.abc import Sequence
from typing import Protocol

EMBEDDING_DIM = 768


class EmbeddingError(Exception):
    """Embedding failed. The message is technical (logs / last_error), never shown to customers."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    """What the app needs from an LLM vendor.

    `generate` is added in step 9. The LLM never has tools or write access
    (hard rule 3): the server stores whatever validated output it returns.
    """

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one EMBEDDING_DIM-float vector per input text, in the same order."""
        ...


def embed_in_batches(
    provider: LLMProvider, texts: Sequence[str], batch_size: int = 50
) -> list[list[float]]:
    """Embed texts in order, batch_size at a time, validating each provider response."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        result = provider.embed(batch)
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
