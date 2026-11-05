from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection


@dataclass(frozen=True)
class ChunkRow:
    position: int
    content: str
    page: int | None
    section: str | None
    embedding: list[float]


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def replace_chunks(
    conn: Connection[dict[str, object]],
    org_id: UUID,
    doc_id: UUID,
    rows: Sequence[ChunkRow],
) -> int:
    """Delete the document's chunks and insert the new set. Returns the number inserted.

    Does not commit and opens no transaction of its own: the caller wraps this in one
    transaction so re-ingest is all-or-nothing (CLAUDE.md rule 8). If the document does not
    belong to org_id nothing is deleted or inserted and 0 is returned (the schema has no
    composite FK, so this check keeps a chunk from carrying a mismatched org_id).
    """
    owned = conn.execute(
        "select 1 from documents where id = %s and org_id = %s", (doc_id, org_id)
    ).fetchone()
    if owned is None:
        return 0
    conn.execute("delete from chunks where org_id = %s and document_id = %s", (org_id, doc_id))
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "insert into chunks (org_id, document_id, content, embedding, page, position, section) "
            "values (%s, %s, %s, %s::vector, %s, %s, %s)",
            [
                (
                    org_id,
                    doc_id,
                    r.content,
                    _vector_literal(r.embedding),
                    r.page,
                    r.position,
                    r.section,
                )
                for r in rows
            ],
        )
    return len(rows)
