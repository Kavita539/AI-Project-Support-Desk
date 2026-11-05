"""The real SQL behind onboarding, against Postgres with the real migrations."""

import re
from uuid import uuid4

import psycopg
import pytest
from psycopg.errors import CheckViolation, InsufficientPrivilege

from app.auth import Identity
from app.db.queries import orgs
from app.db.queries.profiles import get_profile, insert_profile
from app.errors import ApiError
from app.services.onboarding import create_org, get_org_join_code, join_org
from tests.integration.conftest import Conn, World

CODE_SHAPE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")


def _count(conn: Conn, table: str) -> int:
    queries = {
        "orgs": "select count(*) as n from orgs",
        "org_join_codes": "select count(*) as n from org_join_codes",
    }
    row = conn.execute(queries[table]).fetchone()
    assert row is not None
    return int(str(row["n"]))


# --- create_org ---


def test_create_org_makes_org_admin_profile_and_code(conn: Conn, world: World) -> None:
    asha = world.make_user("asha@acme.com")
    result = create_org(conn, asha, "asha@acme.com", "Acme")
    conn.commit()

    assert result.profile.role == "admin"
    assert result.join_code is not None and CODE_SHAPE.match(result.join_code)
    org_row = conn.execute(
        "select name from orgs where id = %s", (result.profile.org_id,)
    ).fetchone()
    assert org_row is not None and org_row["name"] == "Acme"
    profile = get_profile(conn, asha.user_id)
    assert profile is not None
    assert (profile.org_id, profile.role) == (result.profile.org_id, "admin")
    stored = orgs.get_join_code(conn, result.profile.org_id)
    assert stored is not None and stored == result.join_code.replace("-", "")


def test_join_codes_are_unique(conn: Conn, world: World) -> None:
    org_a, org_b = world.make_org("A"), world.make_org("B")
    assert orgs.insert_join_code(conn, org_a, "AAAAAAAA") is True
    assert orgs.insert_join_code(conn, org_b, "AAAAAAAA") is False  # taken: caller retries
    assert orgs.get_join_code(conn, org_b) is None
    conn.commit()


def test_malformed_join_code_is_rejected_by_the_database(conn: Conn, world: World) -> None:
    org = world.make_org()
    with pytest.raises(CheckViolation):
        orgs.insert_join_code(conn, org, "ABC10OIL")  # 0, 1, O, I are not in the alphabet
    conn.rollback()


def test_create_org_is_atomic_when_the_profile_insert_fails(conn: Conn, world: World) -> None:
    asha = world.make_user()
    create_org(conn, asha, "a@example.com", "First")
    conn.commit()
    orgs_before, codes_before = _count(conn, "orgs"), _count(conn, "org_join_codes")
    conn.commit()  # leave conn idle so create_org opens a real transaction, not a savepoint

    # same user again: profile insert violates the PK. No conn.rollback() afterwards: the
    # service's own transaction must undo the org and code inserts, or the counts differ.
    with pytest.raises(ApiError) as err:
        create_org(conn, asha, "a@example.com", "Second")

    assert err.value.status_code == 409
    assert _count(conn, "orgs") == orgs_before
    assert _count(conn, "org_join_codes") == codes_before
    assert conn.execute("select 1 from orgs where name = 'Second'").fetchone() is None


def test_create_org_for_a_deleted_account_leaves_no_org(conn: Conn, world: World) -> None:
    ghost = Identity(user_id=uuid4(), email="ghost@example.com")  # no auth.users row
    before = _count(conn, "orgs")
    conn.commit()  # leave conn idle so create_org opens a real transaction, not a savepoint
    with pytest.raises(ApiError) as err:
        create_org(conn, ghost, "ghost@example.com", "Ghosts Inc")
    assert err.value.status_code == 401
    assert _count(conn, "orgs") == before


# --- join_org ---


def test_join_org_creates_a_customer_in_the_right_org(conn: Conn, world: World) -> None:
    asha, bilal = world.make_user(), world.make_user()
    acme = create_org(conn, asha, "asha@acme.com", "Acme")
    other = create_org(conn, world.make_user(), "o@example.com", "Other")
    conn.commit()
    assert acme.join_code is not None

    joined = join_org(conn, bilal, "bilal@example.com", acme.join_code.lower())
    conn.commit()

    assert joined.join_code is None
    profile = get_profile(conn, bilal.user_id)
    assert profile is not None
    assert profile.role == "customer"
    assert profile.org_id == acme.profile.org_id
    assert profile.org_id != other.profile.org_id


def test_join_with_unknown_code_fails_and_creates_nothing(conn: Conn, world: World) -> None:
    bilal = world.make_user()
    with pytest.raises(ApiError) as err:
        join_org(conn, bilal, "bilal@example.com", "AAAA-BBBB")
    conn.rollback()
    assert err.value.status_code == 404
    assert err.value.code == "invalid_join_code"
    assert get_profile(conn, bilal.user_id) is None


def test_user_cannot_onboard_twice(conn: Conn, world: World) -> None:
    asha = world.make_user()
    acme = create_org(conn, asha, "asha@acme.com", "Acme")
    conn.commit()
    assert acme.join_code is not None

    with pytest.raises(ApiError) as again:
        create_org(conn, asha, "asha@acme.com", "Another")
    conn.rollback()
    assert (again.value.status_code, again.value.code) == (409, "already_onboarded")

    with pytest.raises(ApiError) as rejoin:
        join_org(conn, asha, "asha@acme.com", acme.join_code)
    conn.rollback()
    assert (rejoin.value.status_code, rejoin.value.code) == (409, "already_onboarded")

    profile = get_profile(conn, asha.user_id)
    assert profile is not None and profile.role == "admin"
    assert profile.org_id == acme.profile.org_id


def test_insert_profile_rejects_an_unknown_role(conn: Conn, world: World) -> None:
    user, org = world.make_user(), world.make_org()
    with pytest.raises(CheckViolation):
        insert_profile(conn, user.user_id, org, "superuser", "x@example.com")
    conn.rollback()


# --- get_profile ---


def test_get_profile_returns_org_and_role(conn: Conn, world: World) -> None:
    user, org = world.make_user("u@example.com"), world.make_org()
    insert_profile(conn, user.user_id, org, "agent", "u@example.com")
    conn.commit()
    profile = get_profile(conn, user.user_id)
    assert profile is not None
    assert (profile.user_id, profile.org_id, profile.role, profile.email) == (
        user.user_id,
        org,
        "agent",
        "u@example.com",
    )


def test_get_profile_for_unknown_user_is_none(conn: Conn) -> None:
    assert get_profile(conn, uuid4()) is None


# --- tenancy ---


def test_join_codes_are_scoped_by_org_id(conn: Conn, world: World) -> None:
    a = create_org(conn, world.make_user(), "a@example.com", "Org A")
    b = create_org(conn, world.make_user(), "b@example.com", "Org B")
    lonely = world.make_org("No code")
    conn.commit()

    code_a = get_org_join_code(conn, a.profile.org_id)
    code_b = get_org_join_code(conn, b.profile.org_id)
    assert code_a == a.join_code and code_b == b.join_code
    assert code_a != code_b
    assert orgs.get_join_code(conn, lonely) is None  # never falls back to another org's code
    assert orgs.get_join_code(conn, uuid4()) is None
    # A code resolves to exactly its own org.
    assert orgs.find_org_id_by_join_code(conn, code_a.replace("-", "")) == a.profile.org_id


# --- the join-code table is closed to the API-facing roles ---


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_join_codes_are_not_readable_by_client_roles(
    migrated_db: str, world: World, role: str
) -> None:
    org = world.make_org()
    with psycopg.connect(migrated_db, autocommit=True) as admin:
        admin.execute(
            "insert into org_join_codes (org_id, code) values (%s, %s)", (org, "K7M2QX9P")
        )
        # role is one of the two literals from the parametrize list above
        admin.execute(f"set role {role}")
        try:
            with pytest.raises(InsufficientPrivilege):
                admin.execute("select code from org_join_codes").fetchall()
        finally:
            admin.execute("reset role")
