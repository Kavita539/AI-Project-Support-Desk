from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    storage_bucket: str = "documents"
    gemini_api_key: str = ""
    llm_provider: str = "fake"
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
        required = {"DATABASE_URL": self.database_url, "SUPABASE_URL": self.supabase_url}
        return [name for name, value in required.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
