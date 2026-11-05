from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pypdf import PdfWriter

from app.db.jobs import Job
from app.db.queries.documents import Document
from app.main import app
from app.services.documents import MAX_BYTES, read_limited
from tests.conftest import FakeStorage, TwoOrgs, make_token

URL = "/api/v1/documents"


class FakeDocs:
    """Stands in for app.db.queries.documents and app.db.jobs."""

    def __init__(self) -> None:
        self.docs: dict[UUID, Document] = {}
        self.jobs: list[tuple[UUID, str, dict[str, object]]] = []
        self.fail_insert = False

    def insert_document(
        self,
        _conn: object,
        org_id: UUID,
        doc_id: UUID,
        name: str,
        kind: str,
        storage_path: str,
        size_bytes: int,
        page_count: int | None,
        created_by: UUID,
    ) -> Document:
        if self.fail_insert:
            raise RuntimeError("db down")
        doc = Document(
            doc_id, org_id, name, kind, storage_path, "pending", None, page_count,
            size_bytes, created_by, datetime.now(UTC),
        )  # fmt: skip
        self.docs[doc_id] = doc
        return doc

    def get_document(self, _conn: object, org_id: UUID, doc_id: UUID) -> Document | None:
        doc = self.docs.get(doc_id)
        return doc if doc and doc.org_id == org_id else None

    def enqueue(
        self,
        _conn: object,
        org_id: UUID,
        job_type: str,
        payload: dict[str, object],
        run_after: object = None,
    ) -> Job:
        self.jobs.append((org_id, job_type, payload))
        now = datetime.now(UTC)
        return Job(uuid4(), org_id, job_type, payload, "queued", 0, 3, now, None, None, now)


@pytest.fixture
def docs(monkeypatch: pytest.MonkeyPatch) -> FakeDocs:
    fake = FakeDocs()
    monkeypatch.setattr("app.db.queries.documents.insert_document", fake.insert_document)
    monkeypatch.setattr("app.db.queries.documents.get_document", fake.get_document)
    monkeypatch.setattr("app.db.jobs.enqueue", fake.enqueue)
    return fake


def pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def auth(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user_id)}"}


def post(
    client: TestClient,
    user_id: UUID,
    name: str,
    content: bytes,
    ctype: str = "application/octet-stream",
    **kwargs: object,
) -> Response:
    res: Response = client.post(
        URL,
        files={"file": (name, content, ctype)},
        headers=auth(user_id),
        **kwargs,
    )
    return res


def error_code(res: Response) -> str:
    code: str = res.json()["error"]["code"]
    return code


# --- happy paths ---


@pytest.mark.parametrize(
    ("name", "content", "kind", "pages"),
    [
        ("guide.pdf", pdf_bytes(3), "pdf", 3),
        ("notes.md", b"# Title\n\nhello", "md", None),
        ("faq.TXT", "café text".encode(), "txt", None),
    ],
)
def test_admin_uploads_each_type(
    client: TestClient,
    storage: FakeStorage,
    docs: FakeDocs,
    two_orgs: TwoOrgs,
    name: str,
    content: bytes,
    kind: str,
    pages: int | None,
) -> None:
    res = post(client, two_orgs.a.admin.user_id, name, content)
    assert res.status_code == 201
    body = res.json()
    assert body["kind"] == kind
    assert body["page_count"] == pages
    assert body["size_bytes"] == len(content)
    assert body["status"] == "pending"
    assert "storage_path" not in body
    doc = docs.docs[UUID(body["id"])]
    assert doc.storage_path in storage.objects
    assert storage.objects[doc.storage_path][0] == content
    assert docs.jobs == [(two_orgs.a.org_id, "ingest_document", {"document_id": body["id"]})]


def test_org_and_creator_come_from_the_token_not_the_request(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(
        client,
        two_orgs.a.admin.user_id,
        "a.txt",
        b"hi",
        params={"org_id": str(two_orgs.b.org_id), "role": "admin"},
        data={
            "org_id": str(two_orgs.b.org_id),
            "created_by": str(two_orgs.b.admin.user_id),
            "role": "agent",
        },
    )
    assert res.status_code == 201
    doc = docs.docs[UUID(res.json()["id"])]
    assert doc.org_id == two_orgs.a.org_id
    assert doc.created_by == two_orgs.a.admin.user_id
    assert doc.storage_path is not None
    assert doc.storage_path.startswith(f"{two_orgs.a.org_id}/")


# --- auth ---


@pytest.mark.parametrize("role", ["agent", "customer"])
def test_non_admin_gets_403(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs, role: str
) -> None:
    res = post(client, two_orgs.a.profile(role).user_id, "a.txt", b"hi")
    assert res.status_code == 403
    assert not storage.objects and not docs.docs


def test_no_token_gets_401(client: TestClient, storage: FakeStorage) -> None:
    res = client.post(URL, files={"file": ("a.txt", b"hi", "text/plain")})
    assert res.status_code == 401
    assert not storage.objects


# --- size, type and content checks ---


def test_too_large_is_413_and_states_limit(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "big.txt", b"a" * (MAX_BYTES + 1))
    assert res.status_code == 413
    assert error_code(res) == "file_too_large"
    assert "10 MB" in res.json()["error"]["message"]
    assert not storage.objects and not docs.docs


def test_exactly_at_limit_is_accepted(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "max.txt", b"a" * MAX_BYTES)
    assert res.status_code == 201


class CountingStream:
    """A stream with no length to trust; records how much was actually read."""

    def __init__(self, total: int) -> None:
        self.remaining = total
        self.read_bytes = 0

    def read(self, n: int = -1) -> bytes:
        n = min(n, self.remaining)
        self.remaining -= n
        self.read_bytes += n
        return b"a" * n


def test_read_limited_stops_early_without_trusting_any_length() -> None:
    stream = CountingStream(total=1024 * 1024 * 1024)
    with pytest.raises(Exception) as exc:
        read_limited(stream)  # type: ignore[arg-type]
    assert getattr(exc.value, "code", "") == "file_too_large"
    assert stream.read_bytes < MAX_BYTES + 1024 * 1024  # aborted long before 1 GB


def test_bad_extension_is_415(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    for name in ("a.exe", "noext", "a.pdf.exe"):
        res = post(client, two_orgs.a.admin.user_id, name, b"hi", "application/pdf")
        assert res.status_code == 415
        assert error_code(res) == "unsupported_type"
    assert not storage.objects


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("a.md", pdf_bytes()),  # PDF bytes under a text name (invalid UTF-8 / NUL)
        ("a.txt", b"bin\x00ary"),
        ("a.txt", b"\xff\xfe\xfa"),
        ("a.pdf", b"just some text"),
    ],
)
def test_content_must_match_extension(
    client: TestClient,
    storage: FakeStorage,
    docs: FakeDocs,
    two_orgs: TwoOrgs,
    name: str,
    content: bytes,
) -> None:
    res = post(client, two_orgs.a.admin.user_id, name, content, "text/plain")
    assert res.status_code == 415
    assert error_code(res) == "content_mismatch"
    assert not storage.objects and not docs.docs


def test_empty_file_is_400(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "a.txt", b"")
    assert res.status_code == 400
    assert error_code(res) == "empty_file"


def test_corrupt_pdf_is_400(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "a.pdf", b"%PDF-1.4\nthis is not a pdf")
    assert res.status_code == 400
    assert error_code(res) == "invalid_pdf"
    assert not storage.objects


def test_encrypted_pdf_is_400(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("secret")
    out = BytesIO()
    writer.write(out)
    res = post(client, two_orgs.a.admin.user_id, "a.pdf", out.getvalue())
    assert res.status_code == 400
    assert error_code(res) == "invalid_pdf"


def test_too_many_pages_is_413(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "a.pdf", pdf_bytes(101))
    assert res.status_code == 413
    assert error_code(res) == "too_many_pages"
    assert "100" in res.json()["error"]["message"]
    assert not storage.objects
    assert post(client, two_orgs.a.admin.user_id, "b.pdf", pdf_bytes(100)).status_code == 201


# --- filename and storage path ---


def test_filename_is_sanitised_and_path_is_org_scoped(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "..\\..\\etc/../pass wd?*.txt", b"hi")
    assert res.status_code == 201
    doc = docs.docs[UUID(res.json()["id"])]
    assert ".." not in doc.name and "/" not in doc.name and "\\" not in doc.name
    assert doc.name.endswith(".txt")
    assert doc.storage_path == f"{two_orgs.a.org_id}/{doc.id}/{doc.name}"
    assert doc.storage_path.count("/") == 2


def test_long_filename_is_capped(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post(client, two_orgs.a.admin.user_id, "x" * 500 + ".md", b"hi")
    assert res.status_code == 201
    assert len(res.json()["name"]) <= 100
    assert res.json()["name"].endswith(".md")


# --- failure handling ---


def test_storage_failure_is_502_and_nothing_inserted(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    storage.fail_upload = True
    res = post(client, two_orgs.a.admin.user_id, "a.txt", b"hi")
    assert res.status_code == 502
    assert error_code(res) == "storage_error"
    assert not docs.docs and not docs.jobs


def test_db_failure_deletes_the_stored_object(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    docs.fail_insert = True
    quiet = TestClient(app, raise_server_exceptions=False)  # same overrides; 500 is returned
    res = post(quiet, two_orgs.a.admin.user_id, "a.txt", b"hi")
    assert res.status_code == 500
    assert error_code(res) == "internal_error"
    assert not storage.objects
    assert len(storage.deleted) == 1
    assert storage.deleted[0].startswith(f"{two_orgs.a.org_id}/")


def test_commit_failure_deletes_the_stored_object(
    client: TestClient,
    storage: FakeStorage,
    docs: FakeDocs,
    two_orgs: TwoOrgs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_self: object) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr("tests.conftest.FakeConn.commit", boom)
    quiet = TestClient(app, raise_server_exceptions=False)
    res = post(quiet, two_orgs.a.admin.user_id, "a.txt", b"hi")
    assert res.status_code == 500
    assert not storage.objects and len(storage.deleted) == 1


def test_oversized_content_length_is_413_before_auth(client: TestClient) -> None:
    res = client.post(
        "/api/v1/documents", content=b"x", headers={"content-length": str(50 * 1024 * 1024)}
    )
    assert res.status_code == 413
    assert error_code(res) == "file_too_large"


def test_missing_content_length_is_411(client: TestClient) -> None:
    def body() -> Iterator[bytes]:
        yield b"x"

    res = client.post("/api/v1/documents", content=body())
    assert res.status_code == 411
    assert error_code(res) == "length_required"


# --- GET ---


def test_get_document_ok(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    created = post(client, two_orgs.a.admin.user_id, "a.txt", b"hi").json()
    res = client.get(f"{URL}/{created['id']}", headers=auth(two_orgs.a.admin.user_id))
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == created["id"]
    assert body["error"] is None
    assert "storage_path" not in body


def test_get_unknown_document_is_404(client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs) -> None:
    res = client.get(f"{URL}/{uuid4()}", headers=auth(two_orgs.a.admin.user_id))
    assert res.status_code == 404
    assert error_code(res) == "not_found"


def test_get_other_orgs_document_is_404(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    created = post(client, two_orgs.a.admin.user_id, "a.txt", b"hi").json()
    res = client.get(f"{URL}/{created['id']}", headers=auth(two_orgs.b.admin.user_id))
    assert res.status_code == 404


def test_get_requires_admin(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    created = post(client, two_orgs.a.admin.user_id, "a.txt", b"hi").json()
    res = client.get(f"{URL}/{created['id']}", headers=auth(two_orgs.a.customer.user_id))
    assert res.status_code == 403
