from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.auth import require_role
from app.db.pool import Conn
from app.db.queries.profiles import Profile
from app.llm import get_provider
from app.llm.base import LLMProvider
from app.services import answering

router = APIRouter()

MAX_QUERY_CHARS = 1000
# Stripped, non-empty, and no NUL (Postgres rejects it, which would be a 500).
Query = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=MAX_QUERY_CHARS, pattern=r"^[^\x00]+$"
    ),
]


class AskRequest(BaseModel):
    # extra="forbid": org_id and friends are rejected (422), never read from the body.
    model_config = ConfigDict(extra="forbid")

    query: Query


class CitationResponse(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_name: str
    page: int | None
    section: str | None
    snippet: str


class AskResponse(BaseModel):
    question_id: UUID
    status: str
    answer: str
    citations: list[CitationResponse]


@router.post("/ask")
def ask(
    body: AskRequest,
    profile: Annotated[Profile, Depends(require_role("customer", "admin"))],
    conn: Conn,
    provider: Annotated[LLMProvider, Depends(get_provider)],
) -> AskResponse:
    result = answering.ask(conn, provider, profile, body.query)
    return AskResponse(
        question_id=result.question_id,
        status=result.status,
        answer=result.answer,
        citations=[CitationResponse(**vars(c)) for c in result.citations],
    )
