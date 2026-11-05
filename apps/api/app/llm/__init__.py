from app.config import get_settings
from app.llm.base import EMBEDDING_DIM, EmbeddingError, LLMProvider, embed_in_batches
from app.llm.fake import FakeProvider

__all__ = [
    "EMBEDDING_DIM",
    "EmbeddingError",
    "FakeProvider",
    "LLMProvider",
    "embed_in_batches",
    "get_provider",
]


def get_provider() -> LLMProvider:
    name = get_settings().llm_provider
    if name == "fake":
        return FakeProvider()
    raise RuntimeError(f"LLM provider '{name}' is not available yet; set LLM_PROVIDER=fake")
