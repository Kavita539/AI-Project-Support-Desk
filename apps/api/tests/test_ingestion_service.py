"""Ingestion service tests. Everything is faked (rule 11, no Postgres): an in-memory "world"
stands in for the jobs/documents/chunks query modules, and a counting borrow stands in for
the pool, so we can assert on what is written and when a connection is held."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.config import Settings
from app.db.jobs import Job
from app.db.queries.chunks import ChunkRow
from app.db.queries.documents import Document
from app.llm.base import EMBEDDING_DIM, EmbeddingError
from app.services import ingestion
from app.services.chunking import TextChunk
from app.services.chunking import chunk_pages as real_chunk_pages
from app.services.parsing import ParsedPage, PermanentIngestError, format_faq
from app.services.parsing import parse_document as real_parse_document
from app.storage import StorageObjectNotFound
from tests.conftest import FakeStorage

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeConn:
    def __init__(self, world: World) -> None:
        self.world = world

    def commit(self) -> None:
        self.world.log.append("commit")

    def rollback(self) -> None:
        self.world.log.append("rollback")


class FakeProvider:
    def __init__(self) -> None:
        self.fail_times = 0
        self.retryable = True
        self.calls = 0
        self.world: World | None = None

    def embed(self, texts: Sequence[str], *, kind: str = "document") -> list[list[float]]:
        self.calls += 1
        if self.world is not None:
            self.world.active_at_embed.append(self.world.active)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise EmbeddingError("quota exceeded: secret-detail", retryable=self.retryable)
        return [[0.1] * EMBEDDING_DIM for _ in texts]


@dataclass
class World:
    """In-memory database state plus a call log."""

    org_id: UUID = field(default_factory=uuid4)
    docs: dict[tuple[UUID, UUID], Document] = field(default_factory=dict)
    chunks: dict[UUID, list[ChunkRow]] = field(default_factory=dict)
    job_status: dict[UUID, str] = field(default_factory=dict)
    job_error: dict[UUID, str | None] = field(default_factory=dict)
    job_retryable: dict[UUID, bool] = field(default_factory=dict)
    job_delay: dict[UUID, int] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    get_document_orgs: list[UUID] = field(default_factory=list)
    lock_ok: bool = True
    ready_ok: bool = True
    active: int = 0
    borrows: int = 0
    active_at_download: list[int] = field(default_factory=list)
    active_at_embed: list[int] = field(default_factory=list)
    active_at_parse: list[int] = field(default_factory=list)

    def borrow(self) -> Any:
        @contextmanager
        def cm() -> Iterator[FakeConn]:
            self.borrows += 1
            self.active += 1
            try:
                yield FakeConn(self)
            finally:
                self.active -= 1

        return cm()

    def add_doc(self, kind: str = "pdf", path: str | None = "o/d/f.pdf") -> Document:
        doc = Document(
            id=uuid4(),
            org_id=self.org_id,
            name="f.pdf",
            kind=kind,
            storage_path=path,
            status="queued",
            error=None,
            page_count=None,
            size_bytes=10,
            created_by=None,
            created_at=NOW,
        )
        self.docs[(doc.org_id, doc.id)] = doc
        return doc

    def add_job(
        self,
        doc_id: UUID | None,
        *,
        attempts: int = 1,
        max_attempts: int = 3,
        payload: dict[str, object] | None = None,
        job_type: str = "ingest_document",
    ) -> Job:
        job = Job(
            id=uuid4(),
            org_id=self.org_id,
            type=job_type,
            payload=payload if payload is not None else {"document_id": str(doc_id)},
            status="running",
            attempts=attempts,
            max_attempts=max_attempts,
            run_after=NOW,
            locked_at=NOW,
            last_error=None,
            created_at=NOW,
        )
        self.job_status[job.id] = "running"
        return job

    def doc(self, doc: Document) -> Document | None:
        return self.docs.get((doc.org_id, doc.id))

    # ---- fakes for app.db.jobs / documents / chunks -------------------------------------
    def jobs_ns(self) -> SimpleNamespace:
        def lock_running(_c: object, _org: UUID, _id: UUID, *, attempts: int) -> bool:
            return self.lock_ok

        def complete(_c: object, _org: UUID, job_id: UUID, *, attempts: int) -> bool:
            self.job_status[job_id] = "done"
            return True

        def fail(
            _c: object,
            _org: UUID,
            job_id: UUID,
            error: str,
            *,
            attempts: int,
            retry_delay_seconds: int = 30,
            retryable: bool = True,
        ) -> bool:
            self.job_status[job_id] = "queued" if retryable else "failed"
            self.job_error[job_id] = error
            self.job_retryable[job_id] = retryable
            self.job_delay[job_id] = retry_delay_seconds
            return True

        return SimpleNamespace(lock_running=lock_running, complete=complete, fail=fail)

    def documents_ns(self) -> SimpleNamespace:
        def get_document(_c: object, org_id: UUID, doc_id: UUID) -> Document | None:
            self.get_document_orgs.append(org_id)
            return self.docs.get((org_id, doc_id))

        def mark_processing(_c: object, org_id: UUID, doc_id: UUID) -> bool:
            return self._update(org_id, doc_id, status="processing", error=None)

        def mark_queued(_c: object, org_id: UUID, doc_id: UUID) -> bool:
            return self._update(org_id, doc_id, status="queued")

        def mark_failed(_c: object, org_id: UUID, doc_id: UUID, error: str) -> bool:
            return self._update(org_id, doc_id, status="failed", error=error)

        def mark_ready(_c: object, org_id: UUID, doc_id: UUID, page_count: int | None) -> bool:
            if not self.ready_ok:
                self.docs.pop((org_id, doc_id), None)
                return False
            return self._update(org_id, doc_id, status="ready", error=None, page_count=page_count)

        return SimpleNamespace(
            get_document=get_document,
            mark_processing=mark_processing,
            mark_queued=mark_queued,
            mark_failed=mark_failed,
            mark_ready=mark_ready,
        )

    def _update(self, org_id: UUID, doc_id: UUID, **changes: Any) -> bool:
        doc = self.docs.get((org_id, doc_id))
        if doc is None:
            return False
        self.docs[(org_id, doc_id)] = replace(doc, **changes)
        return True

    def chunks_ns(self) -> SimpleNamespace:
        def replace_chunks(_c: object, _org: UUID, doc_id: UUID, rows: Sequence[ChunkRow]) -> int:
            self.chunks[doc_id] = list(rows)  # delete + insert
            return len(rows)

        return SimpleNamespace(ChunkRow=ChunkRow, replace_chunks=replace_chunks)


@dataclass
class Env:
    world: World
    storage: FakeStorage
    provider: FakeProvider
    settings: Settings

    def run(self, job: Job) -> None:
        ingestion.ingest_job(
            job,
            borrow=self.world.borrow,
            storage=self.storage,
            provider=self.provider,
            settings=self.settings,
        )


def fake_parse(kind: str, data: bytes, **_: Any) -> list[ParsedPage]:
    if data == b"corrupt":
        raise PermanentIngestError("This PDF could not be read. It may be corrupt.")
    if data == b"scanned":
        raise PermanentIngestError("No text could be found in this PDF; it may be scanned.")
    if data == b"boom":
        raise RuntimeError("secret internal detail /srv/path select * from x")
    return [
        ParsedPage(i + 1, text, None) for i, text in enumerate(data.decode().split("|")) if text
    ]


def fake_chunk(pages: Sequence[ParsedPage], **_: Any) -> list[TextChunk]:
    return [TextChunk(i, p.text, p.page, p.section) for i, p in enumerate(pages)]


def fake_embed(provider: Any, texts: Sequence[str], batch_size: int = 50) -> list[list[float]]:
    return provider.embed(texts)  # type: ignore[no-any-return]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    world = World()
    storage = FakeStorage()
    provider = FakeProvider()
    provider.world = world

    real_download = storage.download

    def counting_download(path: str) -> bytes:
        world.active_at_download.append(world.active)
        return real_download(path)

    storage.download = counting_download  # type: ignore[method-assign]

    def counting_parse(kind: str, data: bytes, **kw: Any) -> list[ParsedPage]:
        world.active_at_parse.append(world.active)
        return fake_parse(kind, data, **kw)

    monkeypatch.setattr(ingestion, "jobs", world.jobs_ns())
    monkeypatch.setattr(ingestion, "documents", world.documents_ns())
    monkeypatch.setattr(ingestion, "chunk_queries", world.chunks_ns())
    monkeypatch.setattr(ingestion, "parse_document", counting_parse)
    monkeypatch.setattr(ingestion, "chunk_pages", fake_chunk)
    monkeypatch.setattr(ingestion, "embed_in_batches", fake_embed)
    return Env(world, storage, provider, Settings())


def upload(env: Env, content: bytes, kind: str = "pdf") -> Document:
    doc = env.world.add_doc(kind, "o/d/f.pdf")
    env.storage.upload("o/d/f.pdf", content, "application/pdf")
    return doc


# ---- happy path ----------------------------------------------------------------------------


def test_happy_path(env: Env) -> None:
    doc = upload(env, b"first page|second page")
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "ready" and stored.page_count == 2
    assert [c.content for c in env.world.chunks[doc.id]] == ["first page", "second page"]
    assert env.world.job_status[job.id] == "done"
    assert env.world.get_document_orgs == [job.org_id]


def test_double_run_leaves_one_chunk_set(env: Env) -> None:
    doc = upload(env, b"one|two|three")
    env.run(env.world.add_job(doc.id))
    env.run(env.world.add_job(doc.id))
    assert len(env.world.chunks[doc.id]) == 3
    assert [c.position for c in env.world.chunks[doc.id]] == [0, 1, 2]


def test_no_connection_held_during_slow_work(env: Env) -> None:
    doc = upload(env, b"text")
    env.run(env.world.add_job(doc.id))
    assert env.world.borrows >= 2  # begin + finalize
    assert env.world.active_at_download == [0]
    assert env.world.active_at_parse == [0]
    assert env.world.active_at_embed == [0]
    assert env.world.active == 0


# ---- failures ------------------------------------------------------------------------------


def test_corrupt_file_fails_readably_and_not_retryable(env: Env) -> None:
    doc = upload(env, b"corrupt")
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == "This PDF could not be read. It may be corrupt."
    assert env.world.job_status[job.id] == "failed"
    assert env.world.job_retryable[job.id] is False
    assert doc.id not in env.world.chunks


def test_scanned_pdf_gets_scanned_message(env: Env) -> None:
    doc = upload(env, b"scanned")
    env.run(env.world.add_job(doc.id))
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error is not None and "scanned" in stored.error


def test_empty_chunks_is_permanent_no_text(env: Env) -> None:
    doc = upload(env, b"")
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == ingestion.MSG_NO_TEXT
    assert env.world.job_retryable[job.id] is False


def test_missing_storage_object(env: Env) -> None:
    doc = env.world.add_doc()  # nothing uploaded
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == ingestion.MSG_NOT_FOUND
    assert env.world.job_retryable[job.id] is False


def test_no_path_cannot_be_processed(env: Env) -> None:
    for kind in ("faq", "pdf"):
        doc = env.world.add_doc(kind, None)
        env.run(env.world.add_job(doc.id))
        stored = env.world.doc(doc)
        assert stored is not None and stored.error == ingestion.MSG_CANNOT_PROCESS
        assert stored.status == "failed"


@pytest.fixture
def real_faq_pipeline(env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Use the real parser and chunker (only storage, embeddings and the db stay fake)."""
    monkeypatch.setattr(ingestion, "parse_document", real_parse_document)
    monkeypatch.setattr(ingestion, "chunk_pages", real_chunk_pages)
    return env


def upload_faq(env: Env, question: str, answer: str) -> Document:
    doc = env.world.add_doc("faq", "o/d/faq.txt")
    env.storage.upload("o/d/faq.txt", format_faq(question, answer).encode(), "text/plain")
    return doc


def test_faq_document_reaches_ready_with_one_chunk(real_faq_pipeline: Env) -> None:
    env = real_faq_pipeline
    doc = upload_faq(env, "How do I reset?", "Click reset.\n\nThen wait.")
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "ready" and stored.error is None
    chunks = env.world.chunks[doc.id]
    assert len(chunks) == 1
    assert chunks[0].content == "Q: How do I reset?\nA: Click reset.\n\nThen wait."
    assert chunks[0].page is None and chunks[0].section == "How do I reset?"
    assert env.world.job_status[job.id] == "done"


def test_faq_reingest_leaves_one_chunk(real_faq_pipeline: Env) -> None:
    env = real_faq_pipeline
    doc = upload_faq(env, "Q?", "A.")
    env.run(env.world.add_job(doc.id))
    env.run(env.world.add_job(doc.id))
    assert len(env.world.chunks[doc.id]) == 1
    assert [c.position for c in env.world.chunks[doc.id]] == [0]


def test_malformed_faq_fails_readably(real_faq_pipeline: Env) -> None:
    env = real_faq_pipeline
    doc = env.world.add_doc("faq", "o/d/faq.txt")
    env.storage.upload("o/d/faq.txt", b"Q: no answer here\n", "text/plain")
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == "This FAQ entry is not in the expected format. Please re-create it."
    assert env.world.job_retryable[job.id] is False
    assert doc.id not in env.world.chunks


def test_provider_retryable_error_then_final_attempt(env: Env) -> None:
    doc = upload(env, b"text")
    env.provider.fail_times = 5
    job1 = env.world.add_job(doc.id, attempts=1)
    env.run(job1)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "queued"
    assert env.world.job_status[job1.id] == "queued"
    assert env.world.job_retryable[job1.id] is True
    assert env.world.job_delay[job1.id] == 30

    job2 = env.world.add_job(doc.id, attempts=2)
    env.run(job2)
    assert env.world.job_delay[job2.id] == 120  # exponential backoff

    job3 = env.world.add_job(doc.id, attempts=3, max_attempts=3)
    env.run(job3)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == ingestion.MSG_PROVIDER
    assert env.world.job_status[job3.id] == "failed"
    assert env.world.job_retryable[job3.id] is False


def test_provider_non_retryable_fails_immediately(env: Env) -> None:
    doc = upload(env, b"text")
    env.provider.fail_times = 1
    env.provider.retryable = False
    job = env.world.add_job(doc.id, attempts=1)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == ingestion.MSG_PROVIDER


def test_unexpected_error_is_transient_and_leaks_nothing(env: Env) -> None:
    doc = upload(env, b"boom")
    job = env.world.add_job(doc.id, attempts=3, max_attempts=3)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "failed"
    assert stored.error == ingestion.MSG_UNEXPECTED
    for banned in ("secret", "/srv", "select"):
        assert banned not in (stored.error or "")
    err = env.world.job_error[job.id]
    assert err is not None and err.startswith("RuntimeError") and len(err) <= 500


def test_last_error_truncated_to_500(env: Env) -> None:
    exc = RuntimeError("x" * 2000)
    assert len(ingestion._technical(exc)) == 500


def test_classify() -> None:
    assert ingestion.classify(PermanentIngestError("m")) == (False, "m")
    assert ingestion.classify(StorageObjectNotFound("p")) == (False, ingestion.MSG_NOT_FOUND)
    assert ingestion.classify(EmbeddingError("x", retryable=True)) == (True, ingestion.MSG_PROVIDER)
    assert ingestion.classify(EmbeddingError("x", retryable=False))[0] is False
    assert ingestion.classify(ValueError("x")) == (True, ingestion.MSG_UNEXPECTED)


# ---- payload, tenancy, fencing --------------------------------------------------------------


def test_bad_payload_fails_permanently(env: Env) -> None:
    payloads: list[dict[str, object]] = [{}, {"document_id": "not-a-uuid"}, {"document_id": 5}]
    for payload in payloads:
        job = env.world.add_job(None, payload=payload)
        env.run(job)
        assert env.world.job_status[job.id] == "failed"
        assert env.world.job_error[job.id] == "bad payload"
        assert env.world.job_retryable[job.id] is False


def test_unknown_job_type_fails_permanently(env: Env) -> None:
    doc = upload(env, b"text")
    job = env.world.add_job(doc.id, job_type="something_else")
    env.run(job)
    assert env.world.job_error[job.id] == "unknown job type"
    assert env.world.job_status[job.id] == "failed"


def test_other_orgs_document_is_not_found(env: Env) -> None:
    other = World()
    foreign = other.add_doc()
    job = env.world.add_job(foreign.id)  # payload names a document of another org
    env.run(job)
    assert env.world.job_error[job.id] == "document not found"
    assert env.world.job_status[job.id] == "failed"
    assert env.world.get_document_orgs == [job.org_id]  # org from the job, never the payload
    assert env.world.chunks == {}


def test_payload_org_id_is_ignored(env: Env) -> None:
    doc = upload(env, b"text")
    job = env.world.add_job(doc.id, payload={"document_id": str(doc.id), "org_id": str(uuid4())})
    env.run(job)
    assert env.world.get_document_orgs == [job.org_id]
    assert env.world.job_status[job.id] == "done"


def test_lost_fence_at_start_writes_nothing(env: Env) -> None:
    doc = upload(env, b"text")
    env.world.lock_ok = False
    job = env.world.add_job(doc.id)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "queued"
    assert env.world.chunks == {}
    assert env.world.job_status[job.id] == "running"
    assert env.storage.deleted == []
    assert env.provider.calls == 0


def test_lost_fence_before_finalize_writes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = upload(env, b"text")
    job = env.world.add_job(doc.id)
    jobs_ns = env.world.jobs_ns()
    calls = {"n": 0}

    def lock(_c: object, _o: UUID, _j: UUID, *, attempts: int) -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # fenced in step 1, lost afterwards

    jobs_ns.lock_running = lock
    monkeypatch.setattr(ingestion, "jobs", jobs_ns)
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "processing"
    assert env.world.chunks == {}
    assert env.world.job_status[job.id] == "running"


def test_document_deleted_midway_records_no_error(env: Env) -> None:
    doc = upload(env, b"text")
    env.world.ready_ok = False
    job = env.world.add_job(doc.id)
    env.run(job)
    assert env.world.chunks == {}
    assert env.world.job_status[job.id] == "done"
    assert job.id not in env.world.job_error
    # begin + finalize only: completing the job reuses the finalize connection
    assert env.world.borrows == 2
    assert env.world.log == ["commit", "commit"]


def test_every_borrow_ends_committed_or_rolled_back(env: Env) -> None:
    doc = upload(env, b"corrupt")
    env.run(env.world.add_job(doc.id))
    assert env.world.active == 0
    assert env.world.log[-1] in ("commit", "rollback")


# ---- hardening: things the fakes above would otherwise let slip ------------------------------


def test_happy_path_commits_begin_and_finalize(env: Env) -> None:
    """Without these commits nothing would persist in production (the fake only logs them)."""
    doc = upload(env, b"first|second")
    env.run(env.world.add_job(doc.id))
    assert env.world.log == ["commit", "commit"]


def test_failure_inside_finalize_rolls_back_then_records_failure(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = upload(env, b"first|second")
    job = env.world.add_job(doc.id)

    def broken_replace(*_a: object, **_k: object) -> int:
        raise RuntimeError("insert blew up")

    monkeypatch.setattr(
        ingestion,
        "chunk_queries",
        SimpleNamespace(ChunkRow=ChunkRow, replace_chunks=broken_replace),
    )
    env.run(job)
    # begin commits, finalize rolls back, the retry record commits; never a "done" job
    assert env.world.log == ["commit", "rollback", "commit"]
    assert env.world.job_status[job.id] == "queued"
    assert env.world.job_status[job.id] != "done"


def test_settings_reach_parse_chunk_and_embed(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def parse(kind: str, data: bytes, **kw: Any) -> list[ParsedPage]:
        seen["parse"] = kw
        return fake_parse(kind, data)

    def chunk(pages: Sequence[ParsedPage], **kw: Any) -> list[TextChunk]:
        seen["chunk"] = kw
        return fake_chunk(pages)

    def embed(provider: Any, texts: Sequence[str], batch_size: int = 50) -> list[list[float]]:
        seen["batch_size"] = batch_size
        return fake_embed(provider, texts)

    monkeypatch.setattr(ingestion, "parse_document", parse)
    monkeypatch.setattr(ingestion, "chunk_pages", chunk)
    monkeypatch.setattr(ingestion, "embed_in_batches", embed)
    env.settings = env.settings.model_copy(
        update={
            "parse_deadline_seconds": 7.0,
            "chunk_max_chars": 1234,
            "chunk_overlap_chars": 56,
            "embed_batch_size": 9,
        }
    )
    doc = upload(env, b"one|two")
    env.run(env.world.add_job(doc.id))
    assert seen["parse"] == {"deadline_seconds": 7.0}
    assert seen["chunk"] == {"max_chars": 1234, "overlap_chars": 56}
    assert seen["batch_size"] == 9


def test_job_org_and_attempt_reach_every_fenced_call(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, UUID, int]] = []
    jobs_ns = env.world.jobs_ns()
    chunks_ns = env.world.chunks_ns()
    real_lock, real_complete = jobs_ns.lock_running, jobs_ns.complete

    def lock(c: object, org: UUID, job_id: UUID, *, attempts: int) -> bool:
        calls.append(("lock", org, attempts))
        return real_lock(c, org, job_id, attempts=attempts)  # type: ignore[no-any-return]

    def complete(c: object, org: UUID, job_id: UUID, *, attempts: int) -> bool:
        calls.append(("complete", org, attempts))
        return real_complete(c, org, job_id, attempts=attempts)  # type: ignore[no-any-return]

    real_replace = chunks_ns.replace_chunks

    def replace(c: object, org: UUID, doc_id: UUID, rows: Sequence[ChunkRow]) -> int:
        calls.append(("replace", org, -1))
        return real_replace(c, org, doc_id, rows)  # type: ignore[no-any-return]

    monkeypatch.setattr(
        ingestion,
        "jobs",
        SimpleNamespace(**{**vars(jobs_ns), "lock_running": lock, "complete": complete}),
    )
    monkeypatch.setattr(
        ingestion, "chunk_queries", SimpleNamespace(ChunkRow=ChunkRow, replace_chunks=replace)
    )
    doc = upload(env, b"one|two")
    job = env.world.add_job(doc.id, attempts=2)
    env.run(job)
    assert calls
    assert all(org == job.org_id for _, org, _ in calls)
    assert all(a == 2 for name, _, a in calls if name != "replace")
    assert {name for name, _, _ in calls} == {"lock", "complete", "replace"}


def test_lost_fence_in_failure_path_writes_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = upload(env, b"boom")
    job = env.world.add_job(doc.id)
    jobs_ns = env.world.jobs_ns()
    state = {"n": 0}

    def lock(*_a: object, **_k: object) -> bool:
        state["n"] += 1
        return state["n"] == 1  # owns the job at start, loses it before the failure is recorded

    monkeypatch.setattr(
        ingestion, "jobs", SimpleNamespace(**{**vars(jobs_ns), "lock_running": lock})
    )
    env.run(job)
    stored = env.world.doc(doc)
    assert stored is not None and stored.status == "processing" and stored.error is None
    assert env.world.job_status[job.id] == "running"
