from collections.abc import Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool[Connection[dict[str, object]]] | None = None


def open_pool() -> None:
    global _pool
    _pool = ConnectionPool(
        get_settings().database_url,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def get_conn() -> Iterator[Connection[dict[str, object]]]:
    """FastAPI dependency: borrow a connection for one request, then return it."""
    if _pool is None:
        raise RuntimeError("Database pool is not open")
    with _pool.connection() as conn:
        yield conn
