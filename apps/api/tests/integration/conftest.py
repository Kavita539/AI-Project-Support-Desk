"""Real-Postgres integration tests.

They run only when TEST_DATABASE_URL points at a throwaway Postgres with pgvector
(CI uses the pgvector/pgvector:pg16 service container). Without it every test in this
directory is skipped. The session fixture DROPS and recreates the public and auth
schemas, so it refuses any host other than localhost.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app.auth import Identity

Conn = psycopg.Connection[dict[str, object]]

_HERE = Path(__file__).parent
_MIGRATIONS = Path(__file__).resolve().parents[4] / "supabase" / "migrations"
_SKIP_REASON = "TEST_DATABASE_URL is not set; integration tests need a real Postgres with pgvector"


def _database_url() -> str | None:
    return os.environ.get("TEST_DATABASE_URL") or None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = str(_HERE)
    skip = pytest.mark.skip(reason=_SKIP_REASON)
    for item in items:
        if str(item.path).startswith(here):
            item.add_marker(pytest.mark.integration)
            if _database_url() is None:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _database_url()
    if url is None:
        pytest.skip(_SKIP_REASON)
    assert url is not None
    host = urlparse(url).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail(f"Refusing to reset a non-local test database (host={host!r}).")
    return url


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> Iterator[str]:
    """Fresh schemas with the Supabase stubs and every migration applied, once."""
    migrations = sorted(_MIGRATIONS.glob("*.sql"))
    assert migrations, f"no migrations found in {_MIGRATIONS}"
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade")
        conn.execute("drop schema if exists auth cascade")
        conn.execute("create schema public")
        conn.execute("grant usage on schema public to public")
        scripts = [_HERE / "supabase_stubs.sql", *migrations]
        for path in scripts:
            conn.execute(path.read_text(encoding="utf-8"))
    yield database_url


class World:
    """Creates auth users and remembers everything so teardown can delete it."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.user_ids: list[UUID] = []
        self.org_ids: list[UUID] = []

    def make_user(self, email: str | None = None) -> Identity:
        user_id = uuid4()
        email = email or f"{user_id}@example.com"
        with psycopg.connect(self.url, autocommit=True) as admin:
            admin.execute("insert into auth.users (id, email) values (%s, %s)", (user_id, email))
        self.user_ids.append(user_id)
        return Identity(user_id=user_id, email=email)

    def make_org(self, name: str = "Org") -> UUID:
        org_id = uuid4()
        with psycopg.connect(self.url, autocommit=True) as admin:
            admin.execute("insert into orgs (id, name) values (%s, %s)", (org_id, name))
        self.org_ids.append(org_id)
        return org_id

    def cleanup(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as admin:
            found = admin.execute(
                "select distinct org_id from profiles where user_id = any(%s)", (self.user_ids,)
            ).fetchall()
            org_ids = list({*self.org_ids, *(row[0] for row in found)})
            admin.execute("delete from profiles where user_id = any(%s)", (self.user_ids,))
            admin.execute("delete from orgs where id = any(%s)", (org_ids,))  # codes cascade
            admin.execute("delete from auth.users where id = any(%s)", (self.user_ids,))


@pytest.fixture
def world(migrated_db: str) -> Iterator[World]:
    w = World(migrated_db)
    try:
        yield w
    finally:
        w.cleanup()


@pytest.fixture
def conn(migrated_db: str, world: World) -> Iterator[Conn]:
    """A connection shaped like the API's pool connections (dict rows, no autocommit)."""
    with psycopg.connect(migrated_db, row_factory=dict_row, prepare_threshold=None) as c:
        yield c
        c.rollback()

