import logging
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from psycopg import Connection
from pydantic import BaseModel

from app.config import get_settings
from app.db.queries import questions
from app.db.queries.chunks import ChunkHit
from app.db.queries.profiles import Profile
from app.llm.base import LLMProvider
from app.llm.prompts import ASK_SCHEMA, ASK_SYSTEM, build_ask_prompt
from app.llm.structured import generate_validated
from app.services.retrieval import passes_gate, retrieve

logger = logging.getLogger(__name__)

NO_ANSWER_TEXT = (
    "I don't know the answer to that from the available documents. "
    "You can create a ticket and a team member will help."
)
SNIPPET_CHARS = 300


class ModelAnswer(BaseModel):
    answer: str
    cited_chunk_ids: list[str]


@dataclass(frozen=True)
class Citation:
    chunk_id: UUID
    document_id: UUID
    document_name: str
    page: int | None
    section: str | None
    snippet: str

    def as_json(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "chunk_id": str(self.chunk_id),
            "document_id": str(self.document_id),
        }


@dataclass(frozen=True)
class AskResult:
    question_id: UUID
    status: str  # "answered" or "no_answer"
    answer: str
    citations: list[Citation]


def _snippet(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1].rstrip() + "…"


def verify_citations(cited_ids: list[str], hits: list[ChunkHit]) -> list[ChunkHit]:
    """Keep only cited ids that are in the retrieved set (hard rule 4). Unknown ids, ids from
    anywhere else, and repeats are dropped; the model's order is kept."""
    by_id = {str(h.id): h for h in hits}
    kept: dict[str, ChunkHit] = {}
    for raw in cited_ids:
        hit = by_id.get(raw.strip().lower())
        if hit is not None:
            kept.setdefault(str(hit.id), hit)
    return list(kept.values())


def ask(
    conn: Connection[dict[str, object]], provider: LLMProvider, profile: Profile, query: str
) -> AskResult:
    """Retrieve, gate, generate, verify citations, store the question.

    Any LLMError propagates and nothing is stored, so no partial answer is kept (AC-21).
    """
    settings = get_settings()
    hits = retrieve(conn, provider, profile.org_id, query, settings.retrieval_top_k)
    top_score = hits[0].score if hits else None

    kept: list[ChunkHit] = []
    answer = ""
    if passes_gate(hits, settings.retrieval_min_score):
        prompt = build_ask_prompt(query, [(str(h.id), h.content) for h in hits])
        result = generate_validated(
            provider, prompt, ASK_SCHEMA, ModelAnswer, system=ASK_SYSTEM, temperature=0.2
        )
        answer = result.answer.strip()
        kept = verify_citations(result.cited_chunk_ids, hits) if answer else []
        dropped = len(result.cited_chunk_ids) - len(kept)
        if dropped:
            logger.info("Ask: %d cited chunk id(s) dropped as not retrieved or repeated", dropped)

    answered = bool(kept)
    citations = [
        Citation(h.id, h.document_id, h.document_name, h.page, h.section, _snippet(h.content))
        for h in kept
    ]
    final_answer = answer if answered else NO_ANSWER_TEXT
    status = "answered" if answered else "no_answer"
    question_id = questions.insert_question(
        conn,
        profile.org_id,
        profile.user_id,
        query,
        final_answer,
        status,
        [h.id for h in hits],
        [c.as_json() for c in citations],
        top_score,
    )
    conn.commit()
    return AskResult(question_id, status, final_answer, citations)
