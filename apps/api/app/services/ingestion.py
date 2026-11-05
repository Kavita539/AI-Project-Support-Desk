"""Ingestion of one uploaded document: download, parse, chunk, embed, store.

Rules this module lives by:
- No database connection is held while we talk to storage, parse, or call the embedding
  provider (those can take seconds; a held connection would starve the small pool).
- org_id comes only from the claimed job, never from its payload (hard rule 1).
- Document text is untrusted data. Ingestion makes no generation call, only embeddings
  (hard rule 9).
- Every write is fenced by (job id, attempts): if the job was re-queued and claimed by
  another run meanwhile, this run writes nothing.
- Technical details (exception class, message) go to jobs.last_error and the log only.
  documents.error holds a fixed, readable sentence and never exception text.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import UUID

from psycopg import Connection

from app.config import Settings
from app.db import jobs
from app.db.jobs import Job
from app.db.queries import chunks as chunk_queries
from app.db.queries import documents
from app.db.queries.documents import Document
from app.llm.base import EMBEDDING_DIM, Embedder, EmbeddingError, embed_in_batches
from app.services.chunking import chunk_pages
from app.services.parsing import PermanentIngestError, parse_document
from app.storage import Storage, StorageObjectNotFound

logger = logging.getLogger(__name__)

Conn = Connection[dict[str, object]]
Borrow = Callable[[], AbstractContextManager[Conn]]

JOB_TYPE = "ingest_document"
_MAX_ERROR_CHARS = 500

MSG_CANNOT_PROCESS = "This document type cannot be processed yet."
MSG_NO_TEXT = (
    "No text could be found in this document. If it is a scanned file, upload a text-based version."
)
MSG_NOT_FOUND = "The uploaded file could not be found. Please upload it again."
MSG_PROVIDER = (
    "The AI service was unavailable while processing this document. Please try again later."
)
MSG_UNEXPECTED = "Processing failed unexpectedly. Please try again or contact support."


def classify(exc: BaseException) -> tuple[bool, str]:
    """Return (retryable, user_message) for a failure."""
    if isinstance(exc, PermanentIngestError):
        return False, exc.user_message
    if isinstance(exc, StorageObjectNotFound):
        return False, MSG_NOT_FOUND
    if isinstance(exc, EmbeddingError):
        return exc.retryable, MSG_PROVIDER
    return True, MSG_UNEXPECTED


def _technical(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]


def _document_id(payload: dict[str, object]) -> UUID | None:
    raw = payload.get("document_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def ingest_job(
    job: Job,
    *,
    borrow: Borrow,
    storage: Storage,
    provider: Embedder,
    settings: Settings,
) -> None:
    """Run one claimed ingest job to a final state (done, retried or failed)."""
    try:
        _run(job, borrow, storage, provider, settings)
    except Exception as exc:
        logger.exception("Ingest job %s failed", job.id)
        _record_failure(job, exc, borrow, settings)


def _run(
    job: Job, borrow: Borrow, storage: Storage, provider: Embedder, settings: Settings
) -> None:
    doc = _begin(job, borrow)
    if doc is None:
        return

    if doc.storage_path is None:
        raise PermanentIngestError(MSG_CANNOT_PROCESS)

    # Slow work: no database connection is held from here until finalize.
    data = storage.download(doc.storage_path)
    pages = parse_document(doc.kind, data, deadline_seconds=settings.parse_deadline_seconds)
    chunks = chunk_pages(
        pages,
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
    )
    if not chunks:
        raise PermanentIngestError(MSG_NO_TEXT)
    vectors = embed_in_batches(provider, [c.content for c in chunks], settings.embed_batch_size)
    if len(vectors) != len(chunks) or any(len(v) != EMBEDDING_DIM for v in vectors):
        raise ValueError("embedding provider returned an unexpected shape")

    rows = [
        chunk_queries.ChunkRow(c.position, c.content, c.page, c.section, v)
        for c, v in zip(chunks, vectors, strict=True)
    ]
    page_numbers = {p.page for p in pages if p.page is not None}
    _finalize(job, doc, rows, len(page_numbers) or None, borrow)


def _fail_job_permanently(job: Job, conn: Conn, error: str) -> None:
    jobs.fail(conn, job.org_id, job.id, error, attempts=job.attempts, retryable=False)
    conn.commit()


def _begin(job: Job, borrow: Borrow) -> Document | None:
    """Step 1, one short transaction: validate the job, fence it, mark the document
    processing. Returns None when there is nothing more to do."""
    with borrow() as conn:
        try:
            if job.type != JOB_TYPE:
                _fail_job_permanently(job, conn, "unknown job type")
                return None
            doc_id = _document_id(job.payload)
            if doc_id is None:
                _fail_job_permanently(job, conn, "bad payload")
                return None
            # org_id is the claimed job's, never anything from the payload.
            doc = documents.get_document(conn, job.org_id, doc_id)
            if doc is None:
                _fail_job_permanently(job, conn, "document not found")
                return None
            if not jobs.lock_running(conn, job.org_id, job.id, attempts=job.attempts):
                conn.rollback()
                logger.info("Ingest job %s was taken over; skipping", job.id)
                return None
            documents.mark_processing(conn, job.org_id, doc.id)
            # Explicit commit: conn.transaction() on a connection that already has an open
            # transaction would only create a savepoint and commit nothing.
            conn.commit()
            return doc
        except BaseException:
            conn.rollback()
            raise


def _finalize(
    job: Job,
    doc: Document,
    rows: list[chunk_queries.ChunkRow],
    page_count: int | None,
    borrow: Borrow,
) -> None:
    """Last step, one transaction: chunks, document ready and job done commit together."""
    with borrow() as conn:
        try:
            if not jobs.lock_running(conn, job.org_id, job.id, attempts=job.attempts):
                conn.rollback()
                logger.info("Ingest job %s was taken over before finalize; skipping", job.id)
                return
            if not documents.mark_ready(conn, job.org_id, doc.id, page_count):
                # The document was deleted while we worked. Nothing to attach chunks to;
                # finish the job quietly. Same connection: the row lock is already ours, and a
                # second borrow here could starve the small pool.
                jobs.complete(conn, job.org_id, job.id, attempts=job.attempts)
                conn.commit()
                return
            chunk_queries.replace_chunks(conn, job.org_id, doc.id, rows)
            jobs.complete(conn, job.org_id, job.id, attempts=job.attempts)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _record_failure(job: Job, exc: BaseException, borrow: Borrow, settings: Settings) -> None:
    """Persist the outcome of a failed run: retry later, or fail for good."""
    retryable, message = classify(exc)
    final = (not retryable) or job.attempts >= job.max_attempts
    doc_id = _document_id(job.payload) if job.type == JOB_TYPE else None
    technical = _technical(exc)
    with borrow() as conn:
        try:
            if not jobs.lock_running(conn, job.org_id, job.id, attempts=job.attempts):
                conn.rollback()
                logger.info("Ingest job %s was taken over; not recording failure", job.id)
                return
            if final:
                if doc_id is not None:
                    documents.mark_failed(conn, job.org_id, doc_id, message)
                jobs.fail(
                    conn, job.org_id, job.id, technical, attempts=job.attempts, retryable=False
                )
            else:
                delay = settings.ingest_retry_base_seconds * 4 ** (job.attempts - 1)
                jobs.fail(
                    conn,
                    job.org_id,
                    job.id,
                    technical,
                    attempts=job.attempts,
                    retry_delay_seconds=delay,
                    retryable=True,
                )
                if doc_id is not None:
                    documents.mark_queued(conn, job.org_id, doc_id)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
