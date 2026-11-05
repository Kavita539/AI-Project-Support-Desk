from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, StringConstraints

from app.auth import require_role
from app.db.pool import Conn
from app.db.queries.documents import Document
from app.db.queries.profiles import Profile
from app.services import documents
from app.services.parsing import FAQ_ANSWER_MAX, FAQ_QUESTION_MAX
from app.storage import Storage, get_storage

router = APIRouter()

# Stripped, non-empty, and no NUL (Postgres rejects it, which would be a 500).
_NO_NUL = r"^[^\x00]+$"
Question = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=FAQ_QUESTION_MAX, pattern=_NO_NUL
    ),
]
Answer = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=FAQ_ANSWER_MAX, pattern=_NO_NUL
    ),
]


class DocumentResponse(BaseModel):
    id: UUID
    name: str
    kind: str
    status: str
    size_bytes: int | None
    page_count: int | None
    created_at: datetime


class DocumentDetailResponse(DocumentResponse):
    error: str | None


class DocumentListResponse(BaseModel):
    items: list[DocumentDetailResponse]
    total: int


class FaqRequest(BaseModel):
    # extra="forbid": org_id and friends are rejected (422), never read from the body.
    model_config = ConfigDict(extra="forbid")

    question: Question
    answer: Answer


def _summary(doc: Document) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id,
        name=doc.name,
        kind=doc.kind,
        status=doc.status,
        size_bytes=doc.size_bytes,
        page_count=doc.page_count,
        created_at=doc.created_at,
    )


def _detail(doc: Document) -> DocumentDetailResponse:
    return DocumentDetailResponse(**_summary(doc).model_dump(), error=doc.error)


@router.get("/documents")
def list_documents(
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    items, total = documents.list_documents(conn, admin, limit, offset)
    return DocumentListResponse(items=[_detail(d) for d in items], total=total)


@router.post("/documents", status_code=201)
def upload_document(
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
    storage: Annotated[Storage, Depends(get_storage)],
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    doc = documents.upload_document(conn, storage, admin, file.filename or "", file.file)
    return _summary(doc)


@router.post("/documents/faq", status_code=201)
def create_faq(
    body: FaqRequest,
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
    storage: Annotated[Storage, Depends(get_storage)],
) -> DocumentResponse:
    doc = documents.create_faq(conn, storage, admin, body.question, body.answer)
    return _summary(doc)


@router.get("/documents/{document_id}")
def get_document(
    document_id: UUID,
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
) -> DocumentDetailResponse:
    return _detail(documents.get_document(conn, admin, document_id))


@router.post("/documents/{document_id}/reprocess", status_code=202)
def reprocess_document(
    document_id: UUID,
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
) -> DocumentDetailResponse:
    return _detail(documents.reprocess_document(conn, admin, document_id))


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: UUID,
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
    storage: Annotated[Storage, Depends(get_storage)],
) -> Response:
    documents.delete_document(conn, storage, admin, document_id)
    return Response(status_code=204)
