from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from psycopg import Connection

_COLUMNS = (
    "id, org_id, name, kind, storage_path, status, error, page_count, size_bytes, "
    "created_by, created_at"
)


@dataclass(frozen=True)
class Document:
    id: UUID
    org_id: UUID
    name: str
    kind: str
    storage_path: str | None
    status: str
    error: str | None
    page_count: int | None
    size_bytes: int | None
    created_by: UUID | None
    created_at: datetime


def _to_document(row: dict[str, object]) -> Document:
    page_count = row["page_count"]
    size_bytes = row["size_bytes"]
    created_by = row["created_by"]
    return Document(
        id=UUID(str(row["id"])),
        org_id=UUID(str(row["org_id"])),
        name=str(row["name"]),
        kind=str(row["kind"]),
        storage_path=None if row["storage_path"] is None else str(row["storage_path"]),
        status=str(row["status"]),
        error=None if row["error"] is None else str(row["error"]),
        page_count=None if page_count is None else int(str(page_count)),
        size_bytes=None if size_bytes is None else int(str(size_bytes)),
        created_by=None if created_by is None else UUID(str(created_by)),
        created_at=cast(datetime, row["created_at"]),
    )


def insert_document(
    conn: Connection[dict[str, object]],
    org_id: UUID,
    doc_id: UUID,
    name: str,
    kind: str,
    storage_path: str | None,
    size_bytes: int | None,
    page_count: int | None,
    created_by: UUID | None,
) -> Document:
    """Insert a document in status 'queued'. The caller owns the transaction."""
    row = conn.execute(
        "insert into documents "
        "(id, org_id, name, kind, storage_path, size_bytes, page_count, created_by) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s) "
        f"returning {_COLUMNS}",
        (doc_id, org_id, name, kind, storage_path, size_bytes, page_count, created_by),
    ).fetchone()
    assert row is not None
    return _to_document(row)


def get_document(
    conn: Connection[dict[str, object]], org_id: UUID, doc_id: UUID
) -> Document | None:
    """None when the id does not exist or belongs to another org (callers return 404)."""
    row = conn.execute(
        f"select {_COLUMNS} from documents where id = %s and org_id = %s",
        (doc_id, org_id),
    ).fetchone()
    return None if row is None else _to_document(row)
