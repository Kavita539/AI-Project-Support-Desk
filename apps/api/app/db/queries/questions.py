from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb


def insert_question(
    conn: Connection[dict[str, object]],
    org_id: UUID,
    user_id: UUID,
    query: str,
    answer: str | None,
    status: str,
    retrieved_chunk_ids: list[UUID],
    citations: list[dict[str, Any]],
    top_score: float | None,
) -> UUID:
    """Store one asked question. Does not commit; the caller owns the transaction."""
    row = conn.execute(
        "insert into questions "
        "(org_id, user_id, query, answer, status, retrieved_chunk_ids, citations, top_score) "
        "values (%s, %s, %s, %s, %s, %s::uuid[], %s, %s) returning id",
        (org_id, user_id, query, answer, status, retrieved_chunk_ids, Jsonb(citations), top_score),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))
