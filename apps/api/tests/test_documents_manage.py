"""List, FAQ create, reprocess and delete for documents (fake DB and fake Storage)."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.db.queries.documents import Document
from app.errors import ApiError
from app.main import app
from app.services.parsing import FAQ_ANSWER_MAX, FAQ_QUESTION_MAX, format_faq
from tests.conftest import FakeDocs, FakeJob, FakeStorage, TwoOrgs, make_token

URL = "/api/v1/documents"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def auth(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user_id)}"}


def error_code(res: Response) -> str:
    code: str = res.json()["error"]["code"]
    return code


# --- list ---


def test_list_is_newest_first_with_total_and_error_field(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    org = two_orgs.a.org_id
    docs.seed(org, name="old.txt", created_at=T0)
    docs.seed(
        org,
        name="mid.txt",
        status="failed",
        error="Could not read it",
        created_at=T0 + timedelta(days=1),
    )
    docs.seed(org, name="new.txt", status="processing", created_at=T0 + timedelta(days=2))
    res = client.get(URL, headers=auth(two_orgs.a.admin.user_id))
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 3
    assert [i["name"] for i in body["items"]] == ["new.txt", "mid.txt", "old.txt"]
    failed = body["items"][1]
    assert failed["status"] == "failed"
    assert failed["error"] == "Could not read it"
    assert body["items"][0]["error"] is None
    assert set(failed) == {
        "id", "name", "kind", "status", "error", "page_count", "size_bytes", "created_at",
    }  # fmt: skip


def test_list_limit_and_offset_page_through_but_total_is_the_whole_count(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    for n in range(5):
        docs.seed(two_orgs.a.org_id, name=f"d{n}.txt", created_at=T0 + timedelta(days=n))
    headers = auth(two_orgs.a.admin.user_id)
    first = client.get(URL, params={"limit": 2}, headers=headers).json()
    second = client.get(URL, params={"limit": 2, "offset": 2}, headers=headers).json()
    last = client.get(URL, params={"limit": 2, "offset": 4}, headers=headers).json()
    assert [i["name"] for i in first["items"]] == ["d4.txt", "d3.txt"]
    assert [i["name"] for i in second["items"]] == ["d2.txt", "d1.txt"]
    assert [i["name"] for i in last["items"]] == ["d0.txt"]
    assert first["total"] == second["total"] == last["total"] == 5


def test_list_only_returns_the_callers_org(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    docs.seed(two_orgs.a.org_id, name="a.txt")
    docs.seed(two_orgs.b.org_id, name="b1.txt")
    docs.seed(two_orgs.b.org_id, name="b2.txt")
    res = client.get(
        URL,
        params={"org_id": str(two_orgs.a.org_id)},  # ignored: the org comes from the token
        headers=auth(two_orgs.b.admin.user_id),
    )
    body = res.json()
    assert body["total"] == 2
    assert sorted(i["name"] for i in body["items"]) == ["b1.txt", "b2.txt"]


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "x"}])
def test_list_rejects_bad_paging(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs, params: dict[str, object]
) -> None:
    res = client.get(URL, params=params, headers=auth(two_orgs.a.admin.user_id))
    assert res.status_code == 422
    assert error_code(res) == "invalid_input"


def test_list_accepts_the_limit_bounds(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    headers = auth(two_orgs.a.admin.user_id)
    assert client.get(URL, params={"limit": 1}, headers=headers).status_code == 200
    assert client.get(URL, params={"limit": 100}, headers=headers).status_code == 200


# --- FAQ create ---


def post_faq(client: TestClient, user_id: UUID, body: Mapping[str, object]) -> Response:
    res: Response = client.post(f"{URL}/faq", json=body, headers=auth(user_id))
    return res


def test_faq_is_stored_as_text_and_queued(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    question = "  How do I reset my password?  "
    answer = "Use the reset link.\nIt expires in 1 hour.  "
    res = post_faq(client, two_orgs.a.admin.user_id, {"question": question, "answer": answer})
    assert res.status_code == 201
    body = res.json()
    doc_id = UUID(body["id"])
    path = f"{two_orgs.a.org_id}/{doc_id}/faq.txt"
    expected = format_faq(
        "How do I reset my password?", "Use the reset link.\nIt expires in 1 hour."
    )
    assert storage.objects[path] == (expected.encode("utf-8"), "text/plain")
    doc = docs.docs[doc_id]
    assert (doc.kind, doc.storage_path, doc.org_id) == ("faq", path, two_orgs.a.org_id)
    assert doc.created_by == two_orgs.a.admin.user_id
    assert doc.name == "How do I reset my password?"
    assert doc.size_bytes == len(expected.encode("utf-8"))
    assert doc.page_count is None
    assert body["kind"] == "faq" and body["status"] == "queued" and body["page_count"] is None
    assert "storage_path" not in body
    assert [(j.org_id, j.type, j.payload) for j in docs.jobs] == [
        (two_orgs.a.org_id, "ingest_document", {"document_id": str(doc_id)})
    ]


def test_faq_size_is_utf8_bytes_and_name_is_truncated(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    question = "é" * 150
    res = post_faq(client, two_orgs.a.admin.user_id, {"question": question, "answer": "ü"})
    assert res.status_code == 201
    doc = docs.docs[UUID(res.json()["id"])]
    assert doc.name == "é" * 100
    assert doc.size_bytes == len(format_faq(question, "ü").encode("utf-8"))
    assert doc.size_bytes > len(format_faq(question, "ü"))  # bytes, not characters


def test_faq_name_is_a_single_line(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = post_faq(
        client, two_orgs.a.admin.user_id, {"question": "How do\n I   reset?", "answer": "Click."}
    )
    assert docs.docs[UUID(res.json()["id"])].name == "How do I reset?"


def test_faq_org_comes_from_the_token(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = client.post(
        f"{URL}/faq",
        json={"question": "Q?", "answer": "A."},
        params={"org_id": str(two_orgs.a.org_id)},
        headers=auth(two_orgs.b.admin.user_id),
    )
    assert res.status_code == 201
    doc = docs.docs[UUID(res.json()["id"])]
    assert doc.org_id == two_orgs.b.org_id
    assert next(iter(storage.objects)).startswith(f"{two_orgs.b.org_id}/")
    assert docs.jobs[0].org_id == two_orgs.b.org_id


@pytest.mark.parametrize("extra", ["org_id", "created_by", "kind"])
def test_faq_rejects_unknown_fields(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs, extra: str
) -> None:
    body = {"question": "Q?", "answer": "A.", extra: str(two_orgs.b.org_id)}
    res = post_faq(client, two_orgs.a.admin.user_id, body)
    assert res.status_code == 422
    assert not storage.objects and not docs.docs and not docs.jobs


@pytest.mark.parametrize(
    "body",
    [
        {"question": "x" * (FAQ_QUESTION_MAX + 1), "answer": "A."},
        {"question": "Q?", "answer": "x" * (FAQ_ANSWER_MAX + 1)},
        {"question": "", "answer": "A."},
        {"question": "Q?", "answer": ""},
        {"question": "   ", "answer": "A."},
        {"question": "Q?", "answer": " \n "},
        {"question": "Q\x00?", "answer": "A."},
        {"question": "Q?", "answer": "A\x00."},
        {"question": "Q?"},
        {"answer": "A."},
        {"question": 5, "answer": "A."},
    ],
)
def test_faq_rejects_invalid_input(
    client: TestClient,
    storage: FakeStorage,
    docs: FakeDocs,
    two_orgs: TwoOrgs,
    body: dict[str, object],
) -> None:
    res = post_faq(client, two_orgs.a.admin.user_id, body)
    assert res.status_code == 422
    assert error_code(res) == "invalid_input"
    assert not storage.objects and not docs.docs and not docs.jobs


def test_faq_accepts_exact_length_limits(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    body = {"question": "q" * FAQ_QUESTION_MAX, "answer": "a" * FAQ_ANSWER_MAX}
    assert post_faq(client, two_orgs.a.admin.user_id, body).status_code == 201


def test_faq_storage_failure_is_502_and_nothing_inserted(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    storage.fail_upload = True
    res = post_faq(client, two_orgs.a.admin.user_id, {"question": "Q?", "answer": "A."})
    assert res.status_code == 502
    assert not docs.docs and not docs.jobs


def test_faq_db_failure_deletes_the_stored_object(
    storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs, client: TestClient
) -> None:
    docs.fail_insert = True
    quiet = TestClient(app, raise_server_exceptions=False)  # same overrides; 500 is returned
    res = post_faq(quiet, two_orgs.a.admin.user_id, {"question": "Q?", "answer": "A."})
    assert res.status_code == 500
    assert not storage.objects
    assert len(storage.deleted) == 1
    assert storage.deleted[0].startswith(f"{two_orgs.a.org_id}/")
    assert storage.deleted[0].endswith("/faq.txt")


def test_faq_oversized_body_is_413_before_auth(client: TestClient) -> None:
    res = client.post(f"{URL}/faq", content=b"x" * (37 * 1024))
    assert res.status_code == 413
    assert error_code(res) == "body_too_large"


def test_faq_without_content_length_is_411(client: TestClient) -> None:
    # A chunked body has no Content-Length, so the cap could not be applied: refuse it.
    res = client.post(f"{URL}/faq", content=(b"x" for _ in range(3)))
    assert res.status_code == 411
    assert error_code(res) == "length_required"


def test_upload_keeps_its_larger_cap(client: TestClient) -> None:
    # A 17 KB declared body on the upload path is not refused by the small FAQ cap.
    res = client.post(URL, content=b"x" * (17 * 1024))
    assert res.status_code != 413


@pytest.mark.parametrize("role", ["agent", "customer"])
def test_faq_non_admin_is_403(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs, role: str
) -> None:
    res = post_faq(client, two_orgs.a.profile(role).user_id, {"question": "Q?", "answer": "A."})
    assert res.status_code == 403
    assert not storage.objects and not docs.docs


# --- reprocess ---


def reprocess(client: TestClient, user_id: UUID, doc_id: UUID) -> Response:
    res: Response = client.post(f"{URL}/{doc_id}/reprocess", headers=auth(user_id))
    return res


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_reprocess_requeues_ready_and_failed_documents(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs, status: str
) -> None:
    doc = docs.seed(two_orgs.a.org_id, status=status, error="boom" if status == "failed" else None)
    res = reprocess(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 202
    body = res.json()
    assert body["id"] == str(doc.id)
    assert body["status"] == "queued"
    assert body["error"] is None
    stored = docs.docs[doc.id]
    assert stored.status == "queued" and stored.error is None
    assert [(j.org_id, j.type, j.payload, j.status) for j in docs.jobs] == [
        (two_orgs.a.org_id, "ingest_document", {"document_id": str(doc.id)}, "queued")
    ]


@pytest.mark.parametrize("status", ["queued", "processing"])
def test_reprocess_while_in_flight_is_409_and_adds_no_job(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs, status: str
) -> None:
    doc = docs.seed(two_orgs.a.org_id, status=status)
    res = reprocess(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 409
    assert error_code(res) == "already_processing"
    assert not docs.jobs
    assert docs.docs[doc.id].status == status


def test_reprocess_without_a_stored_file_is_409(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = docs.seed(two_orgs.a.org_id, status="failed", storage_path=None)
    res = reprocess(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 409
    assert error_code(res) == "not_reprocessable"
    assert not docs.jobs
    assert docs.docs[doc.id].status == "failed"


def test_reprocess_unknown_document_is_404(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = reprocess(client, two_orgs.a.admin.user_id, uuid4())
    assert res.status_code == 404
    assert error_code(res) == "not_found"
    assert not docs.jobs


def test_reprocess_other_orgs_document_is_404_and_queues_nothing(
    client: TestClient, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = docs.seed(two_orgs.a.org_id, status="failed", error="boom")
    res = reprocess(client, two_orgs.b.admin.user_id, doc.id)
    assert res.status_code == 404
    assert error_code(res) == "not_found"
    assert not docs.jobs
    assert docs.docs[doc.id].status == "failed"  # untouched


# --- delete ---


def delete(client: TestClient, user_id: UUID, doc_id: UUID) -> Response:
    res: Response = client.delete(f"{URL}/{doc_id}", headers=auth(user_id))
    return res


def stored_doc(storage: FakeStorage, docs: FakeDocs, org_id: UUID) -> Document:
    doc = docs.seed(org_id, name="guide.md", kind="md")
    assert doc.storage_path is not None
    storage.objects[doc.storage_path] = (b"# hi", "text/markdown")
    return doc


def test_delete_removes_the_file_and_the_row(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = stored_doc(storage, docs, two_orgs.a.org_id)
    other = stored_doc(storage, docs, two_orgs.a.org_id)
    res = delete(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 204
    assert res.content == b""
    assert storage.deleted == [f"{two_orgs.a.org_id}/{doc.id}/guide.md"]
    assert doc.id not in docs.docs
    assert other.id in docs.docs  # only the requested document goes
    assert other.storage_path in storage.objects
    assert client.get(f"{URL}/{doc.id}", headers=auth(two_orgs.a.admin.user_id)).status_code == 404


def test_delete_works_in_any_status(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    for status in ("queued", "processing", "ready", "failed"):
        doc = docs.seed(two_orgs.a.org_id, status=status)
        assert delete(client, two_orgs.a.admin.user_id, doc.id).status_code == 204
        assert doc.id not in docs.docs


def test_delete_storage_failure_keeps_the_row_and_is_502(
    client: TestClient,
    storage: FakeStorage,
    docs: FakeDocs,
    two_orgs: TwoOrgs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = stored_doc(storage, docs, two_orgs.a.org_id)
    docs.jobs.append(FakeJob(two_orgs.a.org_id, "ingest_document", {"document_id": str(doc.id)}))

    def broken(_path: str) -> None:
        raise ApiError(502, "storage_error", "File storage is unavailable, please try again.")

    monkeypatch.setattr(storage, "delete", broken)
    res = delete(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 502
    assert error_code(res) == "storage_error"
    assert doc.id in docs.docs
    assert len(docs.jobs) == 1  # the job was not removed either
    monkeypatch.delattr(storage, "delete")  # back to the class method: the admin retries
    assert delete(client, two_orgs.a.admin.user_id, doc.id).status_code == 204
    assert doc.id not in docs.docs


def test_delete_when_the_object_is_already_gone_succeeds(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = docs.seed(two_orgs.a.org_id)  # row exists, nothing in Storage (delete is a no-op)
    assert doc.storage_path not in storage.objects
    res = delete(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 204
    assert storage.deleted == [doc.storage_path]
    assert doc.id not in docs.docs


def test_delete_document_without_a_stored_file_skips_storage(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = docs.seed(two_orgs.a.org_id, kind="faq", storage_path=None)
    res = delete(client, two_orgs.a.admin.user_id, doc.id)
    assert res.status_code == 204
    assert storage.deleted == []
    assert doc.id not in docs.docs


def test_delete_removes_only_this_documents_queued_jobs(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = stored_doc(storage, docs, two_orgs.a.org_id)
    other = stored_doc(storage, docs, two_orgs.a.org_id)
    org = two_orgs.a.org_id
    docs.jobs += [
        FakeJob(org, "ingest_document", {"document_id": str(doc.id)}),
        FakeJob(org, "ingest_document", {"document_id": str(doc.id)}),
        FakeJob(org, "ingest_document", {"document_id": str(doc.id)}, status="running"),
        FakeJob(org, "ingest_document", {"document_id": str(other.id)}),
    ]
    assert delete(client, two_orgs.a.admin.user_id, doc.id).status_code == 204
    assert [(j.payload["document_id"], j.status) for j in docs.jobs] == [
        (str(doc.id), "running"),  # a running job finishes quietly in the worker
        (str(other.id), "queued"),
    ]


def test_delete_unknown_document_is_404(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    res = delete(client, two_orgs.a.admin.user_id, uuid4())
    assert res.status_code == 404
    assert error_code(res) == "not_found"
    assert storage.deleted == []


def test_delete_other_orgs_document_is_404_and_touches_nothing(
    client: TestClient, storage: FakeStorage, docs: FakeDocs, two_orgs: TwoOrgs
) -> None:
    doc = stored_doc(storage, docs, two_orgs.a.org_id)
    docs.jobs.append(FakeJob(two_orgs.a.org_id, "ingest_document", {"document_id": str(doc.id)}))
    res = delete(client, two_orgs.b.admin.user_id, doc.id)
    assert res.status_code == 404
    assert error_code(res) == "not_found"
    assert doc.id in docs.docs  # still there
    assert storage.deleted == []  # Storage untouched
    assert doc.storage_path in storage.objects
    assert len(docs.jobs) == 1


@pytest.mark.parametrize("role", ["agent", "customer"])
def test_delete_and_reprocess_non_admin_is_403_and_change_nothing(
    client: TestClient,
    storage: FakeStorage,
    docs: FakeDocs,
    two_orgs: TwoOrgs,
    role: str,
) -> None:
    doc = stored_doc(storage, docs, two_orgs.a.org_id)
    user = two_orgs.a.profile(role).user_id
    assert delete(client, user, doc.id).status_code == 403
    assert reprocess(client, user, doc.id).status_code == 403
    assert doc.id in docs.docs and docs.docs[doc.id].status == "ready"
    assert storage.deleted == [] and not docs.jobs
