import re
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app.services.onboarding import format_join_code, new_join_code, normalize_join_code
from tests.conftest import FakeDb, make_token

CODE_SHAPE = re.compile(r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def onboard(
    client: TestClient, user_id: UUID, body: dict[str, object], **claims: Any
) -> Response:
    token = make_token(user_id, **claims)
    res: Response = client.post("/api/v1/onboarding", json=body, headers=auth(token))
    return res


# --- creating an org (Asha) ---


def test_create_org_makes_caller_admin_and_returns_join_code(client: TestClient) -> None:
    asha = uuid4()
    res = onboard(client, asha, {"org_name": "Acme"}, email="asha@acme.com")
    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "admin"
    assert body["user_id"] == str(asha)
    assert body["email"] == "asha@acme.com"  # from the token, not the body
    assert CODE_SHAPE.match(body["join_code"])


def test_after_creating_an_org_me_works(client: TestClient) -> None:
    asha = uuid4()
    created = onboard(client, asha, {"org_name": "Acme"}).json()
    me = client.get("/api/v1/me", headers=auth(make_token(asha))).json()
    assert me["org_id"] == created["org_id"]
    assert me["role"] == "admin"


def test_org_name_is_trimmed_and_stored(client: TestClient, db: FakeDb) -> None:
    onboard(client, uuid4(), {"org_name": "  Acme  "})
    assert list(db.orgs.values()) == ["Acme"]


def test_join_code_collision_is_retried(client: TestClient, db: FakeDb) -> None:
    db.reject_next_codes = 2
    res = onboard(client, uuid4(), {"org_name": "Acme"})
    assert res.status_code == 201
    assert len(db.codes) == 1


def test_gives_up_with_503_if_codes_keep_colliding(client: TestClient, db: FakeDb) -> None:
    db.reject_next_codes = 99
    res = onboard(client, uuid4(), {"org_name": "Acme"})
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "try_again"


# --- joining an org (Bilal) ---


def test_join_makes_caller_a_customer_in_that_org(client: TestClient, db: FakeDb) -> None:
    org_id = db.add_org(code="K7M2QX9P")
    bilal = uuid4()
    res = onboard(client, bilal, {"join_code": "K7M2-QX9P"}, email="bilal@example.com")
    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "customer"
    assert body["org_id"] == str(org_id)
    assert body["join_code"] is None  # customers never get the code back


@pytest.mark.parametrize("typed", ["k7m2-qx9p", "K7M2QX9P", " k7m2 qx9p "])
def test_join_code_is_forgiving_about_case_dashes_and_spaces(
    client: TestClient, db: FakeDb, typed: str
) -> None:
    db.add_org(code="K7M2QX9P")
    assert onboard(client, uuid4(), {"join_code": typed}).status_code == 201


def test_wrong_join_code_is_404(client: TestClient, db: FakeDb) -> None:
    db.add_org(code="K7M2QX9P")
    res = onboard(client, uuid4(), {"join_code": "AAAA-BBBB"})
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "invalid_join_code"


# --- the rules that keep it safe ---


def test_onboarding_twice_is_409(client: TestClient, db: FakeDb) -> None:
    db.add_org(code="K7M2QX9P")
    asha = uuid4()
    assert onboard(client, asha, {"org_name": "Acme"}).status_code == 201
    again = onboard(client, asha, {"org_name": "Another"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_onboarded"
    assert onboard(client, asha, {"join_code": "K7M2QX9P"}).status_code == 409


def test_joining_never_grants_a_privileged_role(client: TestClient, db: FakeDb) -> None:
    db.add_org(code="K7M2QX9P")
    res = onboard(client, uuid4(), {"join_code": "K7M2QX9P"})
    assert res.json()["role"] == "customer"


@pytest.mark.parametrize(
    "body",
    [
        {"org_name": "Acme", "org_id": str(uuid4())},
        {"join_code": "K7M2QX9P", "role": "admin"},
        {"org_name": "Acme", "role": "admin"},
        {"org_name": "Acme", "join_code": "K7M2QX9P"},
        {},
        {"org_name": ""},
        {"org_name": "   "},
        {"org_name": "x" * 81},
        {"org_name": "a\u0000b"},
        {"org_name": "line1\nline2"},
        {"org_name": "tab\there"},
        {"org_name": "\t"},
        {"join_code": ""},
    ],
)
def test_bad_bodies_are_rejected_with_422(
    client: TestClient, db: FakeDb, body: dict[str, object]
) -> None:
    db.add_org(code="K7M2QX9P")
    res = onboard(client, uuid4(), body)
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_input"
    assert len(db.profiles) == 0


def test_onboarding_without_token_is_401(client: TestClient) -> None:
    assert client.post("/api/v1/onboarding", json={"org_name": "Acme"}).status_code == 401


def test_onboarding_needs_an_email_in_the_token(client: TestClient, db: FakeDb) -> None:
    res = client.post(
        "/api/v1/onboarding",
        json={"org_name": "Acme"},
        headers=auth(make_token(uuid4(), drop=("email",))),
    )
    assert res.status_code == 400
    assert res.json()["error"]["code"] == "email_required"
    assert len(db.orgs) == 0


# --- GET /org/join-code and require_role ---


def test_admin_can_read_the_join_code(client: TestClient) -> None:
    asha = uuid4()
    created = onboard(client, asha, {"org_name": "Acme"}).json()
    res = client.get("/api/v1/org/join-code", headers=auth(make_token(asha)))
    assert res.status_code == 200
    assert res.json() == {"join_code": created["join_code"]}


@pytest.mark.parametrize("role", ["customer", "agent"])
def test_non_admins_cannot_read_the_join_code(client: TestClient, db: FakeDb, role: str) -> None:
    profile = db.add_profile(role)
    res = client.get("/api/v1/org/join-code", headers=auth(make_token(profile.user_id)))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "forbidden"


def test_join_code_endpoint_needs_a_token_and_a_profile(client: TestClient) -> None:
    assert client.get("/api/v1/org/join-code").status_code == 401
    res = client.get("/api/v1/org/join-code", headers=auth(make_token(uuid4())))
    assert res.status_code == 403
    assert res.json()["error"]["code"] == "no_profile"


def test_admin_only_sees_their_own_orgs_code(client: TestClient, db: FakeDb) -> None:
    acme_admin, other_admin = uuid4(), uuid4()
    acme = onboard(client, acme_admin, {"org_name": "Acme"}).json()
    other = onboard(client, other_admin, {"org_name": "Other"}).json()
    res = client.get("/api/v1/org/join-code", headers=auth(make_token(acme_admin)))
    assert res.json()["join_code"] == acme["join_code"]
    assert res.json()["join_code"] != other["join_code"]


# --- code helpers ---


def test_new_join_codes_are_random_and_readable() -> None:
    codes = {new_join_code() for _ in range(200)}
    assert len(codes) == 200
    assert all(len(c) == 8 and not set(c) & set("01OI") for c in codes)


def test_normalize_and_format_round_trip() -> None:
    assert normalize_join_code("k7m2-qx9p") == "K7M2QX9P"
    assert format_join_code("K7M2QX9P") == "K7M2-QX9P"


# --- gaps found in code review ---


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_user_with_a_profile_cannot_join_another_org(
    client: TestClient, db: FakeDb, role: str
) -> None:
    db.add_org(code="K7M2QX9P")
    existing = db.add_profile(role)
    res = onboard(client, existing.user_id, {"join_code": "K7M2QX9P"})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "already_onboarded"
    assert db.profiles[existing.user_id].org_id == existing.org_id
    assert db.profiles[existing.user_id].role == role


@pytest.mark.parametrize("role", ["admin", "agent", "customer"])
def test_user_with_a_profile_cannot_create_another_org(
    client: TestClient, db: FakeDb, role: str
) -> None:
    existing = db.add_profile(role)
    res = onboard(client, existing.user_id, {"org_name": "Sneaky"})
    assert res.status_code == 409
    assert db.profiles[existing.user_id].role == role


def test_deleted_account_gets_401_when_creating_an_org(client: TestClient, db: FakeDb) -> None:
    ghost = uuid4()
    db.deleted_users.add(ghost)
    res = onboard(client, ghost, {"org_name": "Acme"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "unauthorized"


def test_deleted_account_gets_401_when_joining(client: TestClient, db: FakeDb) -> None:
    db.add_org(code="K7M2QX9P")
    ghost = uuid4()
    db.deleted_users.add(ghost)
    assert onboard(client, ghost, {"join_code": "K7M2QX9P"}).status_code == 401


def test_generated_codes_match_the_database_check_constraint() -> None:
    # Keep in sync with supabase/migrations/20261105000002_org_join_codes_hardening.sql
    shape = re.compile(r"^[A-HJ-NP-Z2-9]{8}$")
    assert all(shape.match(new_join_code()) for _ in range(500))
