import logging
from functools import lru_cache

from app.config import get_settings
from app.llm.base import (
    EMBEDDING_DIM,
    Embedder,
    EmbeddingError,
    EmbedKind,
    LLMAuthError,
    LLMBadRequest,
    LLMBlocked,
    LLMError,
    LLMMalformedOutput,
    LLMProvider,
    LLMRateLimited,
    LLMUnavailable,
    embed_in_batches,
)
from app.llm.fake import FakeProvider
from app.llm.gemini import GeminiProvider

__all__ = [
    "EMBEDDING_DIM",
    "Embedder",
    "EmbedKind",
    "EmbeddingError",
    "FakeProvider",
    "GeminiProvider",
    "LLMAuthError",
    "LLMBadRequest",
    "LLMBlocked",
    "LLMError",
    "LLMMalformedOutput",
    "LLMProvider",
    "LLMRateLimited",
    "LLMUnavailable",
    "clear_provider_cache",
    "embed_in_batches",
    "get_provider",
]

logger = logging.getLogger(__name__)

_fake_warned = False


@lru_cache
def _gemini_provider(
    api_key: str, embed_model: str, generation_model: str, base_url: str, timeout_seconds: float
) -> GeminiProvider:
    return GeminiProvider(
        api_key,
        embed_model=embed_model,
        generation_model=generation_model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


def clear_provider_cache() -> None:
    """Drop the cached Gemini provider (and its httpx client). Tests call this."""
    _gemini_provider.cache_clear()


def get_provider() -> LLMProvider:
    global _fake_warned
    settings = get_settings()
    name = settings.llm_provider
    if name == "fake":
        if not _fake_warned:
            _fake_warned = True
            logger.warning("LLM_PROVIDER=fake: embeddings are NOT real")
        return FakeProvider()
    if name == "gemini":
        api_key = settings.gemini_api_key.strip()
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=gemini requires GEMINI_API_KEY to be set")
        return _gemini_provider(
            api_key,
            settings.gemini_embed_model,
            settings.gemini_generation_model,
            settings.gemini_base_url,
            settings.llm_timeout_seconds,
        )
    raise RuntimeError("LLM_PROVIDER must be set to 'fake' or 'gemini'")
