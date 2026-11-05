"""The real SQL behind chunk replacement, against Postgres with pgvector."""

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest

from app.db.queries.chunks import ChunkRow, replace_chunks, search_chunks
from app.db.queries.documents import insert_document
from app.db.queries.profiles import insert_profile
from app.db.queries.questions import insert_question
from tests.integration.conftest import Conn, World

DIMS = 768


@pytest.fixture
def clean_chunks(world: World, conn: Conn) -> Iterator[None]:
    """chunks and documents reference orgs without cascade; delete them before cleanup."""
    yield
    conn.rollback()
    with psycopg.connect(world.url, autocommit=True) as admin:
        admin.execute("delete from questions where org_id = any(%s)", (world.org_ids,))
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


def _basis(k: int) -> list[float]:
    """A unit vector along axis k: cosine similarity is 1 with itself and 0 with the others."""
    return [1.0 if i == k else 0.0 for i in range(DIMS)]


def _chunk(position: int, axis: int, text: str) -> ChunkRow:
    return ChunkRow(position, text, position + 1, None, _basis(axis))


def test_search_orders_by_cosine_and_respects_limit(
    conn: Conn, world: World, clean_chunks: None
) -> None:
    org = world.make_org()
    doc_id = _make_doc(conn, org)
    replace_chunks(
        conn, org, doc_id, [_chunk(0, 1, "far"), _chunk(1, 0, "exact"), _chunk(2, 2, "x")]
    )
    conn.commit()

    hits = search_chunks(conn, org, _basis(0), 2)
    assert [h.content for h in hits][0] == "exact"
    assert len(hits) == 2
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)
    assert hits[1].score == pytest.approx(0.0, abs=1e-6)
    assert (hits[0].document_id, hits[0].document_name, hits[0].page) == (doc_id, "a.txt", 2)


def test_search_never_returns_another_orgs_chunks(
    conn: Conn, world: World, clean_chunks: None
) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    mine, theirs = _make_doc(conn, org), _make_doc(conn, other)
    replace_chunks(conn, org, mine, [_chunk(0, 1, "mine")])
    replace_chunks(conn, other, theirs, [_chunk(0, 0, "theirs")])  # a perfect match for the query
    conn.commit()

    assert [h.content for h in search_chunks(conn, org, _basis(0), 5)] == ["mine"]
    assert [h.content for h in search_chunks(conn, other, _basis(0), 5)] == ["theirs"]
    assert search_chunks(conn, world.make_org("C"), _basis(0), 5) == []


def test_insert_question_stores_citations_and_chunk_ids(
    conn: Conn, world: World, clean_chunks: None
) -> None:
    org = world.make_org()
    user = world.make_user()
    insert_profile(conn, user.user_id, org, "customer", user.email or "")
    chunk_ids = [uuid4(), uuid4()]
    citations = [{"chunk_id": str(chunk_ids[0]), "snippet": "s"}]
    qid = insert_question(
        conn, org, user.user_id, "q?", "a", "answered", chunk_ids, citations, 0.91
    )
    conn.commit()

    row = conn.execute("select * from questions where id = %s", (qid,)).fetchone()
    assert row is not None
    assert (row["org_id"], row["user_id"], row["status"], row["query"]) == (
        org,
        user.user_id,
        "answered",
        "q?",
    )
    assert row["retrieved_chunk_ids"] == chunk_ids
    assert row["citations"] == citations
    assert row["top_score"] == pytest.approx(0.91, abs=1e-5)

    empty = insert_question(conn, org, user.user_id, "q2", "no", "no_answer", [], [], None)
    conn.commit()
    row2 = conn.execute("select * from questions where id = %s", (empty,)).fetchone()
    assert row2 is not None and row2["retrieved_chunk_ids"] == [] and row2["top_score"] is None
