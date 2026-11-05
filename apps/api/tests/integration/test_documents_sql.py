"""The real SQL behind documents, against Postgres with the real migrations."""

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest

from app.db.queries.documents import get_document, insert_document
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
