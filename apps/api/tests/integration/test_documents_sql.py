"""The real SQL behind documents, against Postgres with the real migrations."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest

from app.db.queries.chunks import ChunkRow, replace_chunks
from app.db.queries.documents import (
    count_documents,
    delete_document,
    get_document,
    get_document_for_update,
    insert_document,
    list_documents,
    mark_failed,
    mark_processing,
    mark_queued,
    mark_ready,
)
from tests.integration.conftest import Conn, World


@pytest.fixture
def clean_documents(world: World, conn: Conn) -> Iterator[None]:
    """world.cleanup() deletes orgs, which documents reference without cascade."""
    yield
    conn.rollback()
    with psycopg.connect(world.url, autocommit=True) as admin:
        admin.execute("delete from documents where org_id = any(%s)", (world.org_ids,))


def test_insert_then_get_document(conn: Conn, world: World, clean_documents: None) -> None:
    org = world.make_org()
    doc_id = uuid4()
    path = f"{org}/{doc_id}.pdf"
    created = insert_document(conn, org, doc_id, "Guide.pdf", "pdf", path, 1234, 7, None)
    conn.commit()

    assert created.id == doc_id and created.org_id == org
    assert created.status == "queued"
    assert (created.name, created.kind, created.size_bytes, created.page_count) == (
        "Guide.pdf",
        "pdf",
        1234,
        7,
    )
    assert created.storage_path == path
    assert created.error is None and created.created_by is None
    assert get_document(conn, org, doc_id) == created


def test_optional_fields_may_be_null(conn: Conn, world: World, clean_documents: None) -> None:
    org = world.make_org()
    doc = insert_document(conn, org, uuid4(), "faq", "faq", None, None, None, None)
    conn.commit()

    assert doc.storage_path is None and doc.size_bytes is None and doc.page_count is None


def test_get_document_from_another_org_returns_none(
    conn: Conn, world: World, clean_documents: None
) -> None:
    org_a, org_b = world.make_org("A"), world.make_org("B")
    doc_id = uuid4()
    insert_document(conn, org_a, doc_id, "a.txt", "txt", None, 1, None, None)
    conn.commit()

    assert get_document(conn, org_b, doc_id) is None
    assert get_document(conn, org_a, doc_id) is not None


def test_get_unknown_document_returns_none(conn: Conn, world: World) -> None:
    org = world.make_org()
    assert get_document(conn, org, uuid4()) is None
    conn.commit()


def _state(conn: Conn, doc_id: UUID) -> dict[str, object]:
    row = conn.execute(
        "select status, error, page_count from documents where id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return row


def test_mark_functions_walk_the_lifecycle(conn: Conn, world: World, clean_documents: None) -> None:
    org = world.make_org()
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, None, None)

    assert mark_processing(conn, org, doc_id) is True
    assert _state(conn, doc_id)["status"] == "processing"
    assert mark_failed(conn, org, doc_id, "boom") is True
    assert _state(conn, doc_id) == {"status": "failed", "error": "boom", "page_count": None}
    assert mark_queued(conn, org, doc_id) is True
    assert _state(conn, doc_id) == {"status": "queued", "error": None, "page_count": None}
    assert mark_ready(conn, org, doc_id, 4) is True
    assert _state(conn, doc_id) == {"status": "ready", "error": None, "page_count": 4}
    conn.commit()


def test_mark_ready_keeps_page_count_when_none(
    conn: Conn, world: World, clean_documents: None
) -> None:
    org = world.make_org()
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.pdf", "pdf", None, 1, 9, None)
    assert mark_ready(conn, org, doc_id, None) is True
    assert _state(conn, doc_id)["page_count"] == 9
    conn.commit()


def test_mark_failed_truncates_error_to_500_chars(
    conn: Conn, world: World, clean_documents: None
) -> None:
    org = world.make_org()
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, None, None)
    assert mark_failed(conn, org, doc_id, "y" * 800) is True
    error = _state(conn, doc_id)["error"]
    assert isinstance(error, str) and len(error) == 500
    conn.commit()


def _seed_three(conn: Conn, org: UUID) -> tuple[UUID, UUID, UUID]:
    """Returns (oldest, tie_low, tie_high) ids. The two newer rows share created_at (one
    transaction), so only the id tiebreak orders them."""
    ids = sorted([uuid4(), uuid4()])
    oldest = uuid4()
    insert_document(conn, org, oldest, "old.txt", "txt", None, 1, None, None)
    for doc_id in ids:
        insert_document(conn, org, doc_id, "new.txt", "txt", None, 1, None, None)
    conn.execute(
        "update documents set created_at = now() - interval '1 day' where id = %s", (oldest,)
    )
    conn.commit()
    return oldest, ids[0], ids[1]


def test_list_documents_is_org_scoped_and_newest_first(
    conn: Conn, world: World, clean_documents: None
) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    oldest, tie_low, tie_high = _seed_three(conn, org)
    foreign = uuid4()
    insert_document(conn, other, foreign, "b.txt", "txt", None, 1, None, None)
    conn.commit()

    listed = list_documents(conn, org, 50, 0)
    assert [d.id for d in listed] == [tie_high, tie_low, oldest]  # created_at desc, id desc
    assert all(d.org_id == org for d in listed)
    assert [d.id for d in list_documents(conn, other, 50, 0)] == [foreign]
    assert list_documents(conn, world.make_org("Empty"), 50, 0) == []
    conn.commit()


def test_list_documents_honours_limit_and_offset(
    conn: Conn, world: World, clean_documents: None
) -> None:
    org = world.make_org()
    oldest, tie_low, tie_high = _seed_three(conn, org)

    assert [d.id for d in list_documents(conn, org, 2, 0)] == [tie_high, tie_low]
    assert [d.id for d in list_documents(conn, org, 2, 2)] == [oldest]
    assert [d.id for d in list_documents(conn, org, 1, 1)] == [tie_low]
    assert list_documents(conn, org, 10, 3) == []
    conn.commit()


def test_count_documents_matches_per_org(conn: Conn, world: World, clean_documents: None) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    _seed_three(conn, org)
    insert_document(conn, other, uuid4(), "b.txt", "txt", None, 1, None, None)
    conn.commit()

    assert count_documents(conn, org) == 3
    assert count_documents(conn, other) == 1
    assert count_documents(conn, world.make_org("Empty")) == 0
    conn.commit()


def test_get_document_for_update_is_org_scoped(
    conn: Conn, world: World, clean_documents: None
) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    doc_id = uuid4()
    created = insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, None, None)
    conn.commit()

    assert get_document_for_update(conn, other, doc_id) is None
    assert get_document_for_update(conn, org, uuid4()) is None
    assert get_document_for_update(conn, org, doc_id) == created
    conn.commit()


def _chunk_count(conn: Conn, doc_id: UUID) -> int:
    row = conn.execute(
        "select count(*) as n from chunks where document_id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return int(str(row["n"]))


def test_delete_document_cascades_chunks(conn: Conn, world: World, clean_documents: None) -> None:
    org = world.make_org()
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, None, None)
    chunk = ChunkRow(
        position=0,
        content="hello",
        page=None,
        section=None,
        embedding=[i / 1000.0 for i in range(768)],
    )
    assert replace_chunks(conn, org, doc_id, [chunk]) == 1
    conn.commit()
    assert _chunk_count(conn, doc_id) == 1

    assert delete_document(conn, org, doc_id) is True
    conn.commit()
    assert get_document(conn, org, doc_id) is None
    assert _chunk_count(conn, doc_id) == 0
    assert delete_document(conn, org, doc_id) is False  # already gone
    conn.commit()


def test_delete_document_is_org_scoped(conn: Conn, world: World, clean_documents: None) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, None, None)
    conn.commit()

    assert delete_document(conn, other, doc_id) is False
    assert delete_document(conn, org, uuid4()) is False
    conn.commit()
    assert get_document(conn, org, doc_id) is not None


def test_mark_functions_are_org_scoped(conn: Conn, world: World, clean_documents: None) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, 3, None)
    conn.commit()

    assert mark_processing(conn, other, doc_id) is False
    assert mark_queued(conn, other, doc_id) is False
    assert mark_failed(conn, other, doc_id, "x") is False
    assert mark_ready(conn, other, doc_id, 99) is False
    assert _state(conn, doc_id) == {"status": "queued", "error": None, "page_count": 3}
    assert mark_processing(conn, org, uuid4()) is False  # unknown id
    conn.commit()
