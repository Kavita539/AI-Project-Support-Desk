import re
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_LLM_PROVIDERS = frozenset({"fake", "gemini"})

# Duplicated from app/llm/gemini.py (MAX_EMBED_BATCH, MAX_EMBED_CHARS): importing app.llm
# here would be a cycle because app.llm imports this module. A test asserts they are equal.
MAX_EMBED_BATCH = 100
MAX_EMBED_CHARS = 8000

_MODEL_NAME = re.compile(r"[A-Za-z0-9._-]+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    storage_bucket: str = "documents"
    gemini_api_key: str = ""
    # No default: "fake" or "gemini" must be chosen explicitly (checked in missing_required).
    llm_provider: str = ""
    gemini_embed_model: str = "gemini-embedding-001"
    gemini_generation_model: str = "gemini-3.5-flash-lite"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    llm_timeout_seconds: float = 30.0
    # Comma-separated browser origins allowed to call the API (the deployed web app).
    cors_origins: str = "http://localhost:5173"

    # Ingestion worker (runs as a thread inside the API process).
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0
    worker_stale_seconds: int = 600
    worker_sweep_seconds: int = 60
    ingest_retry_base_seconds: int = 30
    embed_batch_size: int = 50
    chunk_max_chars: int = 2800
    chunk_overlap_chars: int = 400
    parse_deadline_seconds: float = 60.0

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalise_llm_provider(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @property
    def jwt_issuer(self) -> str:
        return f"{self.supabase_url.rstrip('/')}/auth/v1"

    @property
    def jwks_url(self) -> str:
        return f"{self.jwt_issuer}/.well-known/jwks.json"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def missing_required(self) -> list[str]:
        """Names of settings the API cannot run without."""
        missing = [
            name
            for name, value in (
                ("DATABASE_URL", self.database_url),
                ("SUPABASE_URL", self.supabase_url),
            )
            if not value
        ]
        if self.llm_provider not in SUPPORTED_LLM_PROVIDERS:
            missing.append("LLM_PROVIDER")
        elif self.llm_provider == "gemini" and not self.gemini_api_key.strip():
            missing.append("GEMINI_API_KEY")
        if self.llm_provider == "gemini":
            if not 1 <= self.embed_batch_size <= MAX_EMBED_BATCH:
                missing.append(f"EMBED_BATCH_SIZE (must be 1-{MAX_EMBED_BATCH})")
            if self.chunk_max_chars > MAX_EMBED_CHARS:
                missing.append(f"CHUNK_MAX_CHARS (must be at most {MAX_EMBED_CHARS})")
            if not self.gemini_base_url.startswith("https://"):
                missing.append("GEMINI_BASE_URL (must start with https://)")
            if not _MODEL_NAME.fullmatch(self.gemini_embed_model):
                missing.append("GEMINI_EMBED_MODEL")
            if not _MODEL_NAME.fullmatch(self.gemini_generation_model):
                missing.append("GEMINI_GENERATION_MODEL")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
