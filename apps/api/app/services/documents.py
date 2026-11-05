import logging
import re
from io import BytesIO
from typing import BinaryIO
from uuid import UUID, uuid4

from psycopg import Connection
from pypdf import PdfReader

from app.db import jobs
from app.db.queries import documents as queries
from app.db.queries.documents import Document
from app.db.queries.profiles import Profile
from app.errors import ApiError
from app.storage import Storage

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
MAX_PAGES = 100
_CHUNK = 64 * 1024
_NAME_MAX = 100
_CONTENT_TYPES = {"pdf": "application/pdf", "md": "text/markdown", "txt": "text/plain"}


def read_limited(stream: BinaryIO, limit: int = MAX_BYTES) -> bytes:
    """Read in chunks and give up as soon as the limit is passed, so an oversized or
    dishonestly-labelled upload is never held in memory in full."""
    buf = bytearray()
    while chunk := stream.read(_CHUNK):
        buf += chunk
        if len(buf) > limit:
            raise ApiError(413, "file_too_large", "File is too large. The limit is 10 MB.")
    return bytes(buf)


def sanitise_filename(raw: str) -> str:
    """Drop directory parts and unsafe characters; keep the extension."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).lstrip(".")
    stem, ext = name.rsplit(".", 1) if "." in name else (name, "")
    stem = stem[: _NAME_MAX - (len(ext) + 1 if ext else 0)].strip() or "file"
    return f"{stem}.{ext}" if ext else stem


def _kind(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in _CONTENT_TYPES:
        raise ApiError(
            415, "unsupported_type", "Only PDF, Markdown (.md) and text (.txt) files are accepted."
        )
    return ext


def _check_content(kind: str, data: bytes) -> None:
    mismatch = ApiError(415, "content_mismatch", "The file contents do not match its extension.")
    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise mismatch
        return
    if b"\x00" in data:
        raise mismatch
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise mismatch from None


def _count_pages(data: bytes) -> int:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise ApiError(400, "invalid_pdf", "Encrypted PDFs are not supported.")
        pages = len(reader.pages)
    except ApiError:
        raise
    except Exception:  # untrusted file: any parser failure means "not a readable PDF"
        raise ApiError(400, "invalid_pdf", "This PDF could not be read.") from None
    if pages > MAX_PAGES:
        raise ApiError(413, "too_many_pages", f"PDF has {pages} pages. The limit is {MAX_PAGES}.")
    return pages


def upload_document(
    conn: Connection[dict[str, object]],
    storage: Storage,
    profile: Profile,
    filename: str,
    stream: BinaryIO,
) -> Document:
    """Validate, store the file, then record it and queue ingestion in one transaction.
    org_id and created_by come from the verified profile only."""
    safe_name = sanitise_filename(filename)
    kind = _kind(safe_name)  # cheap type check first, before reading up to 10 MB
    data = read_limited(stream)
    if not data:
        raise ApiError(400, "empty_file", "The file is empty.")
    _check_content(kind, data)
    page_count = _count_pages(data) if kind == "pdf" else None

    doc_id: UUID = uuid4()
    path = f"{profile.org_id}/{doc_id}/{safe_name}"
    storage.upload(path, data, _CONTENT_TYPES[kind])
    try:
        with conn.transaction():
            doc = queries.insert_document(
                conn,
                profile.org_id,
                doc_id,
                safe_name,
                kind,
                path,
                len(data),
                page_count,
                profile.user_id,
            )
            jobs.enqueue(conn, profile.org_id, "ingest_document", {"document_id": str(doc_id)})
        # The auth lookup already opened a transaction, so the block above is only a savepoint.
        # Commit here so a failed commit is also covered by the cleanup below.
        conn.commit()
    except Exception:
        # Do not leave an orphaned file behind; a failed cleanup must not hide the real error.
        try:
            storage.delete(path)
        except Exception:
            logger.warning("Could not delete orphaned object %s", path, exc_info=True)
        raise
    return doc


def get_document(conn: Connection[dict[str, object]], profile: Profile, doc_id: UUID) -> Document:
    doc = queries.get_document(conn, profile.org_id, doc_id)
    if doc is None:
        raise ApiError(404, "not_found", "Document not found.")
    return doc
