import logging
from functools import lru_cache
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.errors import ApiError

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0


class Storage(Protocol):
    def upload(self, path: str, data: bytes, content_type: str) -> None: ...

    def delete(self, path: str) -> None: ...


def _storage_error() -> ApiError:
    return ApiError(502, "storage_error", "File storage is unavailable, please try again.")


class SupabaseStorage:
    """Talks to Supabase Storage's REST API with the service-role key (API only, never
    logged). The bucket is created by hand in the dashboard, not by code."""

    def __init__(self, base_url: str, service_key: str, bucket: str) -> None:
        self._base = base_url.rstrip("/")
        self._bucket = bucket
        self._headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def _url(self, path: str) -> str:
        # Quote each segment so a name with spaces or "?" cannot change the URL shape.
        quoted = "/".join(quote(part, safe="") for part in path.split("/"))
        return f"{self._base}/storage/v1/object/{quote(self._bucket, safe='')}/{quoted}"

    def upload(self, path: str, data: bytes, content_type: str) -> None:
        try:
            res = httpx.post(
                self._url(path),
                content=data,
                headers={**self._headers, "Content-Type": content_type, "x-upsert": "false"},
                timeout=_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            logger.exception("Storage upload failed")
            raise _storage_error() from None
        if res.status_code >= 300:
            logger.error("Storage upload rejected: status %s", res.status_code)
            raise _storage_error()

    def delete(self, path: str) -> None:
        try:
            res = httpx.delete(self._url(path), headers=self._headers, timeout=_TIMEOUT_SECONDS)
        except httpx.HTTPError:
            logger.exception("Storage delete failed")
            raise _storage_error() from None
        # A missing object is fine: delete is used for cleanup and must be repeatable.
        if res.status_code >= 300 and res.status_code != 404:
            logger.error("Storage delete rejected: status %s", res.status_code)
            raise _storage_error()


@lru_cache
def _real_storage() -> SupabaseStorage:
    s = get_settings()
    return SupabaseStorage(s.supabase_url, s.supabase_service_role_key, s.storage_bucket)


def get_storage() -> Storage:
    """Dependency, so tests can swap in an in-memory fake instead of calling Supabase."""
    return _real_storage()
