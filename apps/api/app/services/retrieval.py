from uuid import UUID

from psycopg import Connection

from app.db.queries.chunks import ChunkHit, search_chunks
from app.llm.base import EMBEDDING_DIM, Embedder, EmbeddingError


def retrieve(
    conn: Connection[dict[str, object]], embedder: Embedder, org_id: UUID, text: str, top_k: int
) -> list[ChunkHit]:
    """Embed `text` as a query and return the org's closest chunks, best first.

    `org_id` is explicit and comes from the caller's verified profile, never from the request.
    """
    vectors = embedder.embed([text], kind="query")
    if len(vectors) != 1 or len(vectors[0]) != EMBEDDING_DIM:
        raise EmbeddingError("The embedding provider returned an unexpected shape.", retryable=True)
    return search_chunks(conn, org_id, vectors[0], top_k)


def passes_gate(hits: list[ChunkHit], min_score: float) -> bool:
    """The "I don't know" gate (hard rule 5): true only if the best chunk is similar enough.
    When this is false the caller must not call the LLM."""
    return bool(hits) and hits[0].score >= min_score
