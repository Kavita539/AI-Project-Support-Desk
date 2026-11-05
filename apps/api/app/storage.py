import json
import logging
from functools import lru_cache
from typing import Protocol
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.errors import ApiError

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30.0
# Same as the upload limit (app.services.documents.MAX_BYTES). Duplicated because that
# module imports this one.
_MAX_DOWNLOAD = 10 * 1024 * 1024


class Storage(Protocol):
    def upload(self, path: str, data: bytes, content_type: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def download(self, path: str) -> bytes: ...


class StorageObjectNotFound(Exception):
    """The object is not in the bucket (deleted or never uploaded)."""


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
        # Supabase Storage reports a missing object as 400 with statusCode "404".
        gone = res.status_code == 404 or (
            res.status_code == 400 and _body_says_not_found(res.content)
        )
        if res.status_code >= 300 and not gone:
            logger.error("Storage delete rejected: status %s", res.status_code)
            raise _storage_error()

    def download(self, path: str) -> bytes:
        """Fetch an object. Streams the body so an oversized object is refused early."""
        try:
            with httpx.stream(
                "GET", self._url(path), headers=self._headers, timeout=_TIMEOUT_SECONDS
            ) as res:
                if res.status_code == 404:
                    raise StorageObjectNotFound(path)
                if res.status_code == 400 and _body_says_not_found(res.read()):
                    # Supabase Storage reports a missing object as 400 with statusCode "404".
                    raise StorageObjectNotFound(path)
                if res.status_code >= 300:
                    logger.error("Storage download rejected: status %s", res.status_code)
                    raise _storage_error()
                declared = res.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > _MAX_DOWNLOAD:
                    logger.error("Storage object too large (declared %s bytes)", declared)
                    raise _storage_error()
                buf = bytearray()
                for chunk in res.iter_bytes():
                    buf += chunk
                    if len(buf) > _MAX_DOWNLOAD:
                        logger.error("Storage object exceeded the size limit")
                        raise _storage_error()
                return bytes(buf)
        except httpx.HTTPError:
            logger.exception("Storage download failed")
            raise _storage_error() from None


def _body_says_not_found(body: bytes) -> bool:
    try:
        data = json.loads(body[:4096])
    except ValueError:
        return False
    return isinstance(data, dict) and (
        str(data.get("statusCode")) == "404" or data.get("error") == "not_found"
    )


@lru_cache
def _real_storage() -> SupabaseStorage:
    s = get_settings()
    return SupabaseStorage(s.supabase_url, s.supabase_service_role_key, s.storage_bucket)


def get_storage() -> Storage:
    """Dependency, so tests can swap in an in-memory fake instead of calling Supabase."""
    return _real_storage()
