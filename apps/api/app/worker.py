"""Background worker: a single daemon thread inside the API process that claims jobs from
the Postgres `jobs` table and runs them."""

import logging
import threading
import time
from uuid import UUID

from app.config import Settings, get_settings
from app.db import jobs
from app.db.pool import borrow_conn
from app.db.queries import documents
from app.llm import get_provider
from app.llm.base import Embedder
from app.services.ingestion import JOB_TYPE, Borrow, ingest_job
from app.storage import Storage, get_storage

logger = logging.getLogger(__name__)

MSG_INTERRUPTED = "Processing was interrupted. Please re-upload or reprocess the document."
_ERROR_PAUSE_SECONDS = 1.0


def process_one(
    borrow: Borrow, storage: Storage, provider: Embedder, settings: Settings
) -> bool:
    """Claim and run at most one job. True if a job was run, False if the queue is idle."""
    with borrow() as conn:
        # claim_next is global by design (the worker does not know which org has work);
        # from here on only the claimed job's own org_id is used.
        job = jobs.claim_next(conn)
        conn.commit()
    if job is None:
        return False
    try:
        ingest_job(job, borrow=borrow, storage=storage, provider=provider, settings=settings)
    except Exception:
        # ingest_job records failures itself; this is the last line of defence so one bad
        # job can never kill the loop.
        logger.exception("Job %s crashed outside its error handling", job.id)
    return True


def sweep_stale(borrow: Borrow, settings: Settings) -> int:
    """Re-queue jobs stuck in 'running'. Documents whose job ended up 'failed' for good are
    marked failed too, so users are not left looking at 'processing' forever."""
    with borrow() as conn:
        try:
            stale = jobs.requeue_stale(conn, settings.worker_stale_seconds)
            for item in stale:
                if item.status_after != "failed" or item.type != JOB_TYPE:
                    continue
                raw = item.payload.get("document_id")
                try:
                    doc_id = UUID(str(raw))
                except ValueError:
                    continue
                documents.mark_failed(conn, item.org_id, doc_id, MSG_INTERRUPTED)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return len(stale)


class IngestionWorker:
    def __init__(
        self, borrow: Borrow, storage: Storage, provider: Embedder, settings: Settings
    ) -> None:
        self._borrow = borrow
        self._storage = storage
        self._provider = provider
        self._settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ingestion-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                logger.warning("Ingestion worker did not stop within %.1fs", timeout)
        self._thread = None

    def _loop(self) -> None:
        last_sweep = float("-inf")  # sweep once at startup
        while not self._stop.is_set():
            busy = False
            try:
                now = time.monotonic()
                if now - last_sweep >= self._settings.worker_sweep_seconds:
                    last_sweep = now
                    sweep_stale(self._borrow, self._settings)
                busy = process_one(self._borrow, self._storage, self._provider, self._settings)
            except Exception:
                logger.exception("Worker iteration failed; continuing")
                self._stop.wait(_ERROR_PAUSE_SECONDS)
                continue
            if not busy:
                self._stop.wait(self._settings.worker_poll_seconds)


def build_worker() -> IngestionWorker:
    return IngestionWorker(borrow_conn, get_storage(), get_provider(), get_settings())
