from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from psycopg import Connection
from psycopg.types.json import Jsonb

_COLUMNS = (
    "id, org_id, type, payload, status, attempts, max_attempts, run_after, "
    "locked_at, last_error, created_at"
)


@dataclass(frozen=True)
class Job:
    id: UUID
    org_id: UUID
    type: str
    payload: dict[str, object]
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    locked_at: datetime | None
    last_error: str | None
    created_at: datetime


def _to_job(row: dict[str, object]) -> Job:
    locked_at = row["locked_at"]
    last_error = row["last_error"]
    return Job(
        id=UUID(str(row["id"])),
        org_id=UUID(str(row["org_id"])),
        type=str(row["type"]),
        payload=cast(dict[str, object], row["payload"]),
        status=str(row["status"]),
        attempts=int(str(row["attempts"])),
        max_attempts=int(str(row["max_attempts"])),
        run_after=cast(datetime, row["run_after"]),
        locked_at=None if locked_at is None else cast(datetime, locked_at),
        last_error=None if last_error is None else str(last_error),
        created_at=cast(datetime, row["created_at"]),
    )


def enqueue(
    conn: Connection[dict[str, object]],
    org_id: UUID,
    job_type: str,
    payload: dict[str, object],
    run_after: datetime | None = None,
) -> Job:
    """Add a queued job. run_after None means runnable now. The caller owns the transaction."""
    row = conn.execute(
        "insert into jobs (org_id, type, payload, run_after) "
        "values (%s, %s, %s, coalesce(%s, now())) "
        f"returning {_COLUMNS}",
        (org_id, job_type, Jsonb(payload), run_after),
    ).fetchone()
    assert row is not None
    return _to_job(row)


def claim_next(conn: Connection[dict[str, object]]) -> Job | None:
    """Claim the oldest runnable job in one statement.

    This is the ONE intentionally global query: the worker does not know which org's
    work is next. The returned Job carries org_id, and callers must use that org_id for
    every query they make afterwards. FOR UPDATE SKIP LOCKED means concurrent workers
    never claim the same row and never wait on each other.
    """
    row = conn.execute(
        "update jobs set status = 'running', locked_at = now(), attempts = attempts + 1 "
        "where id = ("
        "  select id from jobs "
        "  where status = 'queued' and run_after <= now() "
        "  order by created_at, id "
        "  for update skip locked "
        "  limit 1"
        ") "
        f"returning {_COLUMNS}"
    ).fetchone()
    return None if row is None else _to_job(row)


def complete(conn: Connection[dict[str, object]], org_id: UUID, job_id: UUID) -> bool:
    """running -> done. False if the job is not running or belongs to another org."""
    cur = conn.execute(
        "update jobs set status = 'done', locked_at = null "
        "where id = %s and org_id = %s and status = 'running'",
        (job_id, org_id),
    )
    return cur.rowcount == 1


def fail(
    conn: Connection[dict[str, object]],
    org_id: UUID,
    job_id: UUID,
    error: str,
    retry_delay_seconds: int = 30,
) -> bool:
    """Record a failure of a running job.

    While attempts < max_attempts the job goes back to 'queued' with run_after pushed
    out by retry_delay_seconds; otherwise it ends as 'failed'. False if the job is not
    running or belongs to another org.
    """
    cur = conn.execute(
        "update jobs set "
        "  status = case when attempts < max_attempts then 'queued' else 'failed' end, "
        "  run_after = case when attempts < max_attempts "
        "    then now() + make_interval(secs => %s::double precision) else run_after end, "
        "  last_error = %s, "
        "  locked_at = null "
        "where id = %s and org_id = %s and status = 'running'",
        (retry_delay_seconds, error, job_id, org_id),
    )
    return cur.rowcount == 1


def requeue_stale(conn: Connection[dict[str, object]], older_than_seconds: int) -> int:
    """Recover jobs whose worker died: running rows locked longer ago than the cutoff go
    back to 'queued' (or 'failed' once attempts >= max_attempts). Returns the count.

    Global like claim_next, because it is a worker-side housekeeping sweep over all orgs.
    """
    cur = conn.execute(
        "update jobs set "
        "  status = case when attempts < max_attempts then 'queued' else 'failed' end, "
        "  last_error = coalesce(last_error, 'worker lost: job was running too long'), "
        "  locked_at = null "
        "where status = 'running' "
        "  and locked_at < now() - make_interval(secs => %s::double precision)",
        (older_than_seconds,),
    )
    return cur.rowcount
