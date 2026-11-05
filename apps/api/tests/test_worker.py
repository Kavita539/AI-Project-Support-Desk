"""Worker loop tests: claiming, AC-9 isolation, stale sweep, thread lifecycle. Fakes only."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app import worker
from app.config import Settings, get_settings
from app.db.jobs import Job, StaleJob
from app.main import app
from tests.test_ingestion_service import Env, upload
from tests.test_ingestion_service import env as env  # fixture re-export  # noqa: F401


def queue_jobs(env: Env, monkeypatch: pytest.MonkeyPatch, queue: list[Job]) -> None:
    def claim_next(_conn: object) -> Job | None:
        return queue.pop(0) if queue else None

    monkeypatch.setattr(worker, "jobs", SimpleNamespace(claim_next=claim_next))


def test_process_one_idle(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    queue_jobs(env, monkeypatch, [])
    assert worker.process_one(env.world.borrow, env.storage, env.provider, env.settings) is False
    assert env.world.log == ["commit"]


def test_failed_job_does_not_block_the_next_one(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = upload(env, b"corrupt")
    good = env.world.add_doc("pdf", "o/good.pdf")
    env.storage.upload("o/good.pdf", b"hello|world", "application/pdf")
    queue = [env.world.add_job(bad.id), env.world.add_job(good.id)]
    queue_jobs(env, monkeypatch, queue)

    args = (env.world.borrow, env.storage, env.provider, env.settings)
    assert worker.process_one(*args) is True
    assert env.world.doc(bad) is not None and env.world.doc(bad).status == "failed"  # type: ignore[union-attr]
    assert worker.process_one(*args) is True
    ready = env.world.doc(good)
    assert ready is not None and ready.status == "ready"
    assert len(env.world.chunks[good.id]) == 2
    assert worker.process_one(*args) is False


def test_process_one_never_raises_out_of_ingest(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue_jobs(env, monkeypatch, [env.world.add_job(uuid4())])

    def explode(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(worker, "ingest_job", explode)
    assert worker.process_one(env.world.borrow, env.storage, env.provider, env.settings) is True


def test_sweep_marks_documents_failed_only_for_failed_jobs(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = env.world.add_doc()
    retried = env.world.add_doc()
    triage_doc = env.world.add_doc()

    def stale(job_type: str, doc: UUID, after: str) -> StaleJob:
        return StaleJob(uuid4(), env.world.org_id, job_type, {"document_id": str(doc)}, after)

    items = [
        stale("ingest_document", dead.id, "failed"),
        stale("ingest_document", retried.id.__class__(str(retried.id)), "queued"),
        stale("triage_ticket", triage_doc.id, "failed"),
        StaleJob(uuid4(), env.world.org_id, "ingest_document", {"document_id": "junk"}, "failed"),
    ]
    seen: list[int] = []

    def requeue_stale(_c: object, older_than_seconds: int) -> list[StaleJob]:
        seen.append(older_than_seconds)
        return items

    monkeypatch.setattr(worker, "jobs", SimpleNamespace(requeue_stale=requeue_stale))
    monkeypatch.setattr(worker, "documents", env.world.documents_ns())

    assert worker.sweep_stale(env.world.borrow, env.settings) == 4
    assert seen == [env.settings.worker_stale_seconds]
    failed = env.world.doc(dead)
    assert failed is not None and failed.status == "failed"
    assert failed.error == worker.MSG_INTERRUPTED
    assert env.world.doc(retried).status == "queued"  # type: ignore[union-attr]
    assert env.world.doc(triage_doc).status == "queued"  # type: ignore[union-attr]
    assert env.world.log[-1] == "commit"


def test_sweep_rolls_back_on_error(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    def requeue_stale(_c: object, _s: int) -> list[StaleJob]:
        raise RuntimeError("db down")

    monkeypatch.setattr(worker, "jobs", SimpleNamespace(requeue_stale=requeue_stale))
    with pytest.raises(RuntimeError):
        worker.sweep_stale(env.world.borrow, env.settings)
    assert env.world.log == ["rollback"]


# ---- thread lifecycle ----------------------------------------------------------------------


def fast_settings() -> Settings:
    return Settings(worker_poll_seconds=0.01, worker_sweep_seconds=3600)


def test_worker_survives_an_exception_and_stops(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    survived = threading.Event()

    def flaky(*_a: Any, **_k: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("one bad iteration")
        if calls["n"] >= 3:
            survived.set()
        return False

    monkeypatch.setattr(worker, "process_one", flaky)
    monkeypatch.setattr(worker, "sweep_stale", lambda *_a, **_k: 0)
    monkeypatch.setattr(worker, "_ERROR_PAUSE_SECONDS", 0.01)
    w = worker.IngestionWorker(env.world.borrow, env.storage, env.provider, fast_settings())
    w.start()
    try:
        assert survived.wait(5.0), "loop did not continue after an exception"
    finally:
        start = time.monotonic()
        w.stop(timeout=5.0)
    assert time.monotonic() - start < 5.0
    assert w._thread is None
    assert not any(t.name == "ingestion-worker" for t in threading.enumerate())


def test_worker_sweeps_at_startup_and_is_single_thread(
    env: Env,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swept = threading.Event()

    def sweep(*_a: Any, **_k: Any) -> int:
        swept.set()
        return 0

    monkeypatch.setattr(worker, "sweep_stale", sweep)
    monkeypatch.setattr(worker, "process_one", lambda *_a, **_k: False)
    w = worker.IngestionWorker(env.world.borrow, env.storage, env.provider, fast_settings())
    w.start()
    w.start()  # second start is a no-op
    try:
        assert swept.wait(5.0)
        assert sum(t.name == "ingestion-worker" for t in threading.enumerate()) == 1
    finally:
        w.stop(timeout=5.0)


def test_lifespan_without_worker_starts_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    # The autouse fixture sets WORKER_ENABLED=false.
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    get_settings.cache_clear()
    opened: list[str] = []
    built: list[str] = []
    monkeypatch.setattr("app.main.open_pool", lambda: opened.append("open"))
    monkeypatch.setattr("app.main.close_pool", lambda: opened.append("close"))
    monkeypatch.setattr("app.main.build_worker", lambda: built.append("built"))
    with TestClient(app) as c:
        assert c.get("/api/v1/health").status_code == 200
        assert not any(t.name == "ingestion-worker" for t in threading.enumerate())
    assert built == [] and opened == ["open", "close"]
    get_settings.cache_clear()


def test_lifespan_stops_worker_before_closing_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    get_settings.cache_clear()
    order: list[str] = []

    class StubWorker:
        def start(self) -> None:
            order.append("start")

        def stop(self) -> None:
            order.append("stop")

    monkeypatch.setattr("app.main.open_pool", lambda: order.append("open"))
    monkeypatch.setattr("app.main.close_pool", lambda: order.append("close"))
    monkeypatch.setattr("app.main.build_worker", lambda: StubWorker())
    with TestClient(app):
        pass
    assert order == ["open", "start", "stop", "close"]
    get_settings.cache_clear()


def test_bad_provider_does_not_stop_the_api_booting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    get_settings.cache_clear()

    def broken() -> None:
        raise RuntimeError("LLM provider 'gemini' is not configured")

    monkeypatch.setattr("app.main.open_pool", lambda: None)
    monkeypatch.setattr("app.main.close_pool", lambda: None)
    monkeypatch.setattr("app.main.build_worker", broken)
    with TestClient(app) as c:
        assert c.get("/api/v1/health").status_code == 200
    get_settings.cache_clear()
