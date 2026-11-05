from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import Depends
from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.config import get_settings
from app.errors import ApiError

_pool: ConnectionPool[Connection[dict[str, object]]] | None = None


def open_pool() -> None:
    global _pool
    _pool = ConnectionPool(
        get_settings().database_url,
        min_size=1,
        max_size=5,
        # prepare_threshold=None: Supabase's transaction pooler (port 6543) breaks
        # psycopg's automatic prepared statements.
        kwargs={"row_factory": dict_row, "prepare_threshold": None},
        open=True,
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def borrow_conn() -> Iterator[Connection[dict[str, object]]]:
    """Borrow a pooled connection for non-request code (the worker). The connection goes
    back to the pool clean: any transaction still open is rolled back."""
    if _pool is None:
        raise RuntimeError("Database pool is not open.")
    with _pool.connection(timeout=10.0) as conn:
        try:
            yield conn
        finally:
            if conn.info.transaction_status != TransactionStatus.IDLE:
                conn.rollback()


def get_conn() -> Iterator[Connection[dict[str, object]]]:
    """FastAPI dependency: borrow a connection for one request, then return it."""
    if _pool is None:
        raise ApiError(503, "db_unavailable", "Database is unavailable.")
    try:
        with _pool.connection() as conn:
            yield conn
    except PoolTimeout:
        raise ApiError(503, "db_unavailable", "Database is busy, try again.") from None


# Use this type in routes and dependencies instead of Depends(get_conn) directly.
# scope="function" makes FastAPI return the connection (which commits any open
# transaction) BEFORE the response is sent. The default would commit after the client
# already received a 201, so an immediate follow-up request could miss the new row.
Conn = Annotated[Connection[dict[str, object]], Depends(get_conn, scope="function")]
