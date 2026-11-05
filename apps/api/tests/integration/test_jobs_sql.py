"""The real SQL behind the jobs queue, against Postgres with the real migrations."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from app.db import jobs
from tests.integration.conftest import Conn, World


@pytest.fixture
def clean_jobs(world: World, conn: Conn) -> Iterator[None]:
    """world.cleanup() deletes orgs, which jobs reference without cascade. This runs
    before the conn fixture closes, so release any row locks the test left behind first."""
    yield
    conn.rollback()
    with psycopg.connect(world.url, autocommit=True) as admin:
        admin.execute("delete from jobs where org_id = any(%s)", (world.org_ids,))


def _row(conn: Conn, job_id: UUID) -> dict[str, object]:
    row = conn.execute("select * from jobs where id = %s", (job_id,)).fetchone()
    assert row is not None
    return row


def _hour_from_now() -> datetime:
    return datetime.now(UTC) + timedelta(hours=1)


def test_enqueue_stores_a_queued_job(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    job = jobs.enqueue(conn, org, "ingest_document", {"document_id": "abc"})
    conn.commit()

    assert job.org_id == org and job.type == "ingest_document"
    assert job.payload == {"document_id": "abc"}
    assert (job.status, job.attempts, job.max_attempts) == ("queued", 0, 3)
    assert job.locked_at is None and job.last_error is None


def test_claim_next_runs_the_job_once(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    queued = jobs.enqueue(conn, org, "triage_ticket", {"ticket_id": "t1"})
    conn.commit()

    claimed = jobs.claim_next(conn)
    conn.commit()
    assert claimed is not None
    assert claimed.id == queued.id and claimed.org_id == org
    assert claimed.status == "running" and claimed.attempts == 1
    assert claimed.locked_at is not None and claimed.payload == {"ticket_id": "t1"}

    assert jobs.claim_next(conn) is None  # no double claim
    conn.commit()


def test_claim_next_takes_oldest_first(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    first = jobs.enqueue(conn, org, "triage_ticket", {})
    conn.commit()  # separate transactions so created_at differs
    second = jobs.enqueue(conn, org, "triage_ticket", {})
    conn.commit()

    one = jobs.claim_next(conn)
    two = jobs.claim_next(conn)
    conn.commit()
    assert one is not None and two is not None
    assert (one.id, two.id) == (first.id, second.id)


def test_future_run_after_is_not_claimable(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    jobs.enqueue(conn, org, "triage_ticket", {}, run_after=_hour_from_now())
    conn.commit()

    assert jobs.claim_next(conn) is None
    conn.commit()


def test_skip_locked_second_connection_gets_a_different_job(
    conn: Conn, world: World, clean_jobs: None
) -> None:
    org = world.make_org()
    j1 = jobs.enqueue(conn, org, "triage_ticket", {})
    j2 = jobs.enqueue(conn, org, "triage_ticket", {})
    conn.commit()  # idle, so the claim below opens a real transaction we hold open

    with psycopg.connect(world.url, row_factory=dict_row, prepare_threshold=None) as conn2:
        conn2.execute("set lock_timeout = '3s'")  # waiting on a locked row would raise
        conn2.commit()

        a = jobs.claim_next(conn)  # NOT committed: the row stays locked
        assert a is not None
        b = jobs.claim_next(conn2)
        assert b is not None and b.id != a.id
        assert {a.id, b.id} == {j1.id, j2.id}

        # both rows are locked by open transactions: skipped, not waited on
        assert jobs.claim_next(conn2) is None
        conn2.commit()

    conn.rollback()  # undo conn's claim: the row is claimable again, attempts back to 0
    again = jobs.claim_next(conn)
    conn.commit()
    assert again is not None and again.id == a.id and again.attempts == 1


def test_locked_only_job_is_skipped_not_waited_on(
    conn: Conn, world: World, clean_jobs: None
) -> None:
    org = world.make_org()
    jobs.enqueue(conn, org, "triage_ticket", {})
    conn.commit()

    with psycopg.connect(world.url, row_factory=dict_row, prepare_threshold=None) as conn2:
        conn2.execute("set lock_timeout = '3s'")
        conn2.commit()
        assert jobs.claim_next(conn) is not None  # uncommitted
        assert jobs.claim_next(conn2) is None
        conn2.commit()
    conn.rollback()


def test_fail_retries_then_ends_failed(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    queued = jobs.enqueue(conn, org, "ingest_document", {})
    conn.commit()

    for attempt in (1, 2):
        claimed = jobs.claim_next(conn)
        assert claimed is not None and claimed.attempts == attempt
        assert jobs.fail(conn, org, claimed.id, f"boom {attempt}", retry_delay_seconds=0) is True
        row = _row(conn, queued.id)
        assert row["status"] == "queued" and row["last_error"] == f"boom {attempt}"
        assert row["locked_at"] is None
        conn.commit()  # now() is the transaction start: commit so run_after <= now() holds

    last = jobs.claim_next(conn)
    assert last is not None and last.attempts == 3
    assert jobs.fail(conn, org, last.id, "boom 3", retry_delay_seconds=0) is True
    row = _row(conn, queued.id)
    assert row["status"] == "failed" and row["last_error"] == "boom 3"
    conn.commit()
    assert jobs.claim_next(conn) is None
    conn.commit()


def test_fail_delays_the_retry(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    jobs.enqueue(conn, org, "ingest_document", {})
    conn.commit()
    claimed = jobs.claim_next(conn)
    assert claimed is not None
    assert jobs.fail(conn, org, claimed.id, "later", retry_delay_seconds=3600) is True
    conn.commit()

    assert jobs.claim_next(conn) is None  # run_after is an hour away
    conn.commit()


def test_complete_marks_done_and_is_org_scoped(conn: Conn, world: World, clean_jobs: None) -> None:
    org, other = world.make_org("A"), world.make_org("B")
    jobs.enqueue(conn, org, "triage_ticket", {})
    conn.commit()
    claimed = jobs.claim_next(conn)
    assert claimed is not None

    assert jobs.complete(conn, other, claimed.id) is False  # wrong org
    assert jobs.fail(conn, other, claimed.id, "x") is False  # wrong org
    assert _row(conn, claimed.id)["status"] == "running"
    assert jobs.complete(conn, org, claimed.id) is True
    assert _row(conn, claimed.id)["status"] == "done"
    assert jobs.complete(conn, org, claimed.id) is False  # no longer running
    assert jobs.fail(conn, org, claimed.id, "x") is False
    conn.commit()


def test_complete_ignores_queued_jobs(conn: Conn, world: World, clean_jobs: None) -> None:
    org = world.make_org()
    queued = jobs.enqueue(conn, org, "triage_ticket", {})
    assert jobs.complete(conn, org, queued.id) is False
    assert _row(conn, queued.id)["status"] == "queued"
    conn.commit()


def test_requeue_stale_only_touches_old_running_rows(
    conn: Conn, world: World, clean_jobs: None
) -> None:
    org = world.make_org()
    stale = jobs.enqueue(conn, org, "triage_ticket", {})
    fresh = jobs.enqueue(conn, org, "triage_ticket", {})
    exhausted = jobs.enqueue(conn, org, "triage_ticket", {})
    waiting = jobs.enqueue(conn, org, "triage_ticket", {}, run_after=_hour_from_now())
    conn.commit()

    for _ in range(3):  # claims stale, fresh and exhausted; waiting is not runnable yet
        assert jobs.claim_next(conn) is not None
    conn.execute(
        "update jobs set locked_at = now() - interval '1 hour' where id = any(%s)",
        ([stale.id, exhausted.id],),
    )
    conn.execute("update jobs set attempts = max_attempts where id = %s", (exhausted.id,))
    conn.commit()

    assert jobs.requeue_stale(conn, older_than_seconds=600) == 2
    conn.commit()

    assert _row(conn, stale.id)["status"] == "queued"
    assert _row(conn, stale.id)["locked_at"] is None
    assert _row(conn, exhausted.id)["status"] == "failed"
    assert _row(conn, fresh.id)["status"] == "running"
    assert _row(conn, waiting.id)["status"] == "queued"
    assert jobs.requeue_stale(conn, older_than_seconds=600) == 0
    conn.commit()
