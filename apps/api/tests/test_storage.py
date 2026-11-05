from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from app.errors import ApiError
from app.storage import StorageObjectNotFound, SupabaseStorage
from tests.conftest import FakeStorage

KEY = "service-role-secret"


def make(handler: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> SupabaseStorage:
    """Route the module's httpx calls through a mock transport: no network."""

    def via_mock(method: str, url: str, **kw: object) -> httpx.Response:
        with httpx.Client(transport=handler) as c:
            return c.request(method, url, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("app.storage.httpx.post", lambda url, **kw: via_mock("POST", url, **kw))
    monkeypatch.setattr("app.storage.httpx.delete", lambda url, **kw: via_mock("DELETE", url, **kw))
    return SupabaseStorage("https://x.supabase.co/", KEY, "documents")


def test_upload_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"Key": "ok"})

    store = make(httpx.MockTransport(handler), monkeypatch)
    store.upload("org/doc id/my file?.pdf", b"data", "application/pdf")
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == (
        "https://x.supabase.co/storage/v1/object/documents/org/doc%20id/my%20file%3F.pdf"
    )
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["apikey"] == KEY
    assert req.headers["content-type"] == "application/pdf"
    assert req.content == b"data"


def test_upload_rejection_is_502_without_leaking_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make(httpx.MockTransport(lambda r: httpx.Response(400, text="bad")), monkeypatch)
    with pytest.raises(ApiError) as exc:
        store.upload("a/b/c.txt", b"x", "text/plain")
    assert exc.value.status_code == 502 and exc.value.code == "storage_error"
    assert KEY not in exc.value.message


def test_network_error_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    store = make(httpx.MockTransport(boom), monkeypatch)
    with pytest.raises(ApiError) as exc:
        store.upload("a/b/c.txt", b"x", "text/plain")
    assert exc.value.status_code == 502


def test_delete_tolerates_missing_object(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make(httpx.MockTransport(lambda r: httpx.Response(404)), monkeypatch)
    store.delete("a/b/c.txt")


def test_delete_failure_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make(httpx.MockTransport(lambda r: httpx.Response(500)), monkeypatch)
    with pytest.raises(ApiError):
        store.delete("a/b/c.txt")


def make_download(handler: httpx.MockTransport, monkeypatch: pytest.MonkeyPatch) -> SupabaseStorage:
    @contextmanager
    def via_mock(method: str, url: str, **kw: object) -> Iterator[httpx.Response]:
        with httpx.Client(transport=handler) as c, c.stream(method, url, **kw) as res:  # type: ignore[arg-type]
            yield res

    monkeypatch.setattr("app.storage.httpx.stream", via_mock)
    return SupabaseStorage("https://x.supabase.co/", KEY, "documents")


def test_download_request_shape_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"file-bytes")

    store = make_download(httpx.MockTransport(handler), monkeypatch)
    assert store.download("org/doc id/a b.pdf") == b"file-bytes"
    req = seen[0]
    assert req.method == "GET"
    assert str(req.url) == (
        "https://x.supabase.co/storage/v1/object/documents/org/doc%20id/a%20b.pdf"
    )
    assert req.headers["authorization"] == f"Bearer {KEY}"


def test_download_404_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_download(httpx.MockTransport(lambda r: httpx.Response(404)), monkeypatch)
    with pytest.raises(StorageObjectNotFound):
        store.download("a/b/c.pdf")


def test_download_500_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_download(httpx.MockTransport(lambda r: httpx.Response(500)), monkeypatch)
    with pytest.raises(ApiError) as exc:
        store.download("a/b/c.pdf")
    assert exc.value.status_code == 502 and exc.value.code == "storage_error"


def test_download_network_error_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    store = make_download(httpx.MockTransport(boom), monkeypatch)
    with pytest.raises(ApiError) as exc:
        store.download("a/b/c.pdf")
    assert exc.value.status_code == 502


def test_download_refuses_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    big = b"x" * (10 * 1024 * 1024 + 1)
    handler = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
    store = make_download(handler, monkeypatch)
    with pytest.raises(ApiError):
        store.download("a/b/c.pdf")


def test_fake_storage_download() -> None:
    fake = FakeStorage()
    fake.upload("p", b"data", "text/plain")
    assert fake.download("p") == b"data"
    with pytest.raises(StorageObjectNotFound):
        fake.download("missing")


def test_download_400_with_not_found_body_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"statusCode": "404", "error": "not_found", "message": "Object not found"}
    store = make_download(
        httpx.MockTransport(lambda r: httpx.Response(400, json=body)), monkeypatch
    )
    with pytest.raises(StorageObjectNotFound):
        store.download("a/b/c.pdf")


def test_download_other_400_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    store = make_download(
        httpx.MockTransport(lambda r: httpx.Response(400, text="nope")), monkeypatch
    )
    with pytest.raises(ApiError) as exc:
        store.download("a/b/c.pdf")
    assert exc.value.status_code == 502
