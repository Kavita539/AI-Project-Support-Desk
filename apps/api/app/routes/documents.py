from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from app.auth import require_role
from app.db.pool import Conn
from app.db.queries.documents import Document
from app.db.queries.profiles import Profile
from app.services import documents
from app.storage import Storage, get_storage

router = APIRouter()


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


@router.post("/documents", status_code=201)
def upload_document(
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
    storage: Annotated[Storage, Depends(get_storage)],
    file: Annotated[UploadFile, File()],
) -> DocumentResponse:
    doc = documents.upload_document(conn, storage, admin, file.filename or "", file.file)
    return _summary(doc)


@router.get("/documents/{document_id}")
def get_document(
    document_id: UUID,
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
) -> DocumentDetailResponse:
    doc = documents.get_document(conn, admin, document_id)
    return DocumentDetailResponse(**_summary(doc).model_dump(), error=doc.error)
