"""The real SQL behind chunk replacement, against Postgres with pgvector."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest

from app.db.queries.chunks import ChunkRow, replace_chunks
from app.db.queries.documents import insert_document
from tests.integration.conftest import Conn, World

DIMS = 768


@pytest.fixture
def clean_chunks(world: World, conn: Conn) -> Iterator[None]:
    """chunks and documents reference orgs without cascade; delete them before cleanup."""
    yield
    conn.rollback()
    with psycopg.connect(world.url, autocommit=True) as admin:
        admin.execute("delete from chunks where org_id = any(%s)", (world.org_ids,))
        admin.execute("delete from documents where org_id = any(%s)", (world.org_ids,))


def _vec(seed: float) -> list[float]:
    return [seed + i / 1000.0 for i in range(DIMS)]


def _rows(n: int) -> list[ChunkRow]:
    return [
        ChunkRow(position=i, content=f"chunk {i}", page=i + 1, section=f"s{i}", embedding=_vec(i))
        for i in range(n)
    ]


def _make_doc(conn: Conn, org: UUID) -> UUID:
    doc_id = uuid4()
    insert_document(conn, org, doc_id, "a.txt", "txt", None, 1, None, None)
    conn.commit()
    return doc_id


def _positions(conn: Conn, doc_id: UUID) -> list[int]:
    found = conn.execute(
        "select position from chunks where document_id = %s order by position", (doc_id,)
    ).fetchall()
    return [int(str(r["position"])) for r in found]


def test_replace_twice_leaves_exactly_one_set(conn: Conn, world: World, clean_chunks: None) -> None:
    org = world.make_org()
    doc_id = _make_doc(conn, org)

    assert replace_chunks(conn, org, doc_id, _rows(3)) == 3
    conn.commit()
    assert replace_chunks(conn, org, doc_id, _rows(3)) == 3
    conn.commit()

    assert _positions(conn, doc_id) == [0, 1, 2]


def test_replace_with_fewer_rows_drops_the_rest(
    conn: Conn, world: World, clean_chunks: None
) -> None:
    org = world.make_org()
    doc_id = _make_doc(conn, org)

    replace_chunks(conn, org, doc_id, _rows(5))
    conn.commit()
    assert replace_chunks(conn, org, doc_id, _rows(3)) == 3
    conn.commit()
    assert _positions(conn, doc_id) == [0, 1, 2]

    assert replace_chunks(conn, org, doc_id, []) == 0  # empty set clears the document
    conn.commit()
    assert _positions(conn, doc_id) == []


def test_replace_is_org_scoped(conn: Conn, world: World, clean_chunks: None) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    doc_id = _make_doc(conn, org)
    replace_chunks(conn, org, doc_id, _rows(2))
    conn.commit()

    # another org naming this doc id deletes nothing and inserts nothing
    assert replace_chunks(conn, other, doc_id, _rows(4)) == 0
    conn.commit()

    assert _positions(conn, doc_id) == [0, 1]
    found = conn.execute(
        "select distinct org_id from chunks where document_id = %s", (doc_id,)
    ).fetchall()
    assert [r["org_id"] for r in found] == [org]


def test_vector_round_trips_with_metadata(conn: Conn, world: World, clean_chunks: None) -> None:
    org = world.make_org()
    doc_id = _make_doc(conn, org)
    row = ChunkRow(position=0, content="hello", page=None, section=None, embedding=_vec(0.5))
    replace_chunks(conn, org, doc_id, [row])
    conn.commit()

    stored = conn.execute(
        "select content, page, section, vector_dims(embedding) as dims, "
        "embedding::text as emb from chunks where document_id = %s",
        (doc_id,),
    ).fetchone()
    assert stored is not None
    assert (stored["content"], stored["page"], stored["section"]) == ("hello", None, None)
    assert stored["dims"] == DIMS
    values = [float(x) for x in str(stored["emb"]).strip("[]").split(",")]
    assert len(values) == DIMS
    assert values[0] == pytest.approx(0.5, abs=1e-5)
    assert values[-1] == pytest.approx(0.5 + (DIMS - 1) / 1000.0, abs=1e-4)


def test_wrong_dimension_embedding_is_rejected(
    conn: Conn, world: World, clean_chunks: None
) -> None:
    org = world.make_org()
    doc_id = _make_doc(conn, org)
    bad = ChunkRow(position=0, content="x", page=None, section=None, embedding=[0.1, 0.2])
    with pytest.raises(psycopg.Error):
        replace_chunks(conn, org, doc_id, [bad])
    conn.rollback()
