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


@dataclass(frozen=True)
class ChunkHit:
    id: UUID
    document_id: UUID
    document_name: str
    content: str
    page: int | None
    section: str | None
    score: float  # cosine similarity, 1 is identical


def search_chunks(
    conn: Connection[dict[str, object]], org_id: UUID, query_embedding: list[float], limit: int
) -> list[ChunkHit]:
    """The `limit` chunks of this org closest to the query by cosine distance, best first.

    `<=>` is pgvector's cosine distance, so similarity is `1 - distance`. The org filter is
    on the chunk and again on the document, so a chunk can never be returned for another org.
    """
    vector = _vector_literal(query_embedding)
    rows = conn.execute(
        "select c.id, c.document_id, d.name as document_name, c.content, c.page, c.section, "
        "1 - (c.embedding <=> %s::vector) as score "
        "from chunks c join documents d on d.id = c.document_id and d.org_id = c.org_id "
        "where c.org_id = %s and c.embedding is not null "
        "order by c.embedding <=> %s::vector "
        "limit %s",
        (vector, org_id, vector, limit),
    ).fetchall()
    return [
        ChunkHit(
            id=UUID(str(r["id"])),
            document_id=UUID(str(r["document_id"])),
            document_name=str(r["document_name"]),
            content=str(r["content"]),
            page=None if r["page"] is None else int(str(r["page"])),
            section=None if r["section"] is None else str(r["section"]),
            score=float(str(r["score"])),
        )
        for r in rows
    ]
