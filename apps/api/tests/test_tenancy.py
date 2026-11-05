"""Tenancy and access-control tests driven by tests/endpoint_matrix.py (fake DB)."""

import uuid
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app

from .conftest import FakeDb, TwoOrgs, make_forged_token, make_token
from .endpoint_matrix import ROUTES, Route

PREFIX = "/api/v1"
ALL_ROLES = ("admin", "agent", "customer")

AUTHED = [r for r in ROUTES if not r.public]
GATED = [(r, role) for r in AUTHED if r.roles for role in ALL_ROLES if role not in r.roles]
ALLOWED = [(r, role) for r in AUTHED if r.roles for role in r.roles]


def _id(route: Route) -> str:
    return f"{route.method} {route.path}"


def _pair_ids(pairs: list[tuple[Route, str]]) -> list[str]:
    return [f"{_id(r)} as {role}" for r, role in pairs]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _call(client: TestClient, route: Route, token: str | None) -> httpx.Response:
    path = route.path.format(**route.path_params)
    headers = _bearer(token) if token is not None else {}
    resp: httpx.Response = client.request(route.method, path, headers=headers, json=route.body)
    return resp


def _assert_error_shape(resp: httpx.Response) -> None:
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert all(isinstance(v, str) and v for v in body["error"].values())


def _digits(code: str) -> str:
    return code.replace("-", "")


def _registered_routes() -> list[tuple[str, set[str]]]:
    """(full path, methods) for every route, including routers added with include_router.
    Newer FastAPI keeps those as lazy _IncludedRouter objects, so expand them."""
    found: list[tuple[str, set[str]]] = []
    for route in app.routes:
        contexts: Any = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            found.extend((ctx.path, set(ctx.methods)) for ctx in contexts())
        elif isinstance(route, APIRoute):
            found.append((route.path, set(route.methods or ())))
        # Anything else (docs pages, mounts) is not an API route.
    return found


def test_every_registered_route_is_in_the_matrix() -> None:
    registered = {
        (method, path)
        for path, methods in _registered_routes()
        if path.startswith(PREFIX)
        for method in methods - {"HEAD", "OPTIONS"}
    }
    in_matrix = {(r.method, r.path) for r in ROUTES}
    assert registered - in_matrix == set(), "add these routes to tests/endpoint_matrix.py"
    assert in_matrix - registered == set(), "matrix rows with no real route"
    assert len(in_matrix) == len(ROUTES), "duplicate matrix rows"


@pytest.mark.parametrize("route", AUTHED, ids=_id)
def test_no_token_is_401(client: TestClient, route: Route) -> None:
    resp = _call(client, route, None)
    assert resp.status_code == 401
    _assert_error_shape(resp)


@pytest.mark.parametrize("route", AUTHED, ids=_id)
@pytest.mark.parametrize("kind", ["garbage", "forged", "expired"])
def test_invalid_token_is_401(client: TestClient, route: Route, kind: str) -> None:
    uid = uuid.uuid4()
    token = {
        "garbage": "not-a-jwt",
        "forged": make_forged_token(uid),
        "expired": make_token(uid, expires_in=-3600),
    }[kind]
    resp = _call(client, route, token)
    assert resp.status_code == 401
    _assert_error_shape(resp)


@pytest.mark.parametrize(("route", "role"), GATED, ids=_pair_ids(GATED))
def test_wrong_role_is_403(client: TestClient, two_orgs: TwoOrgs, route: Route, role: str) -> None:
    resp = _call(client, route, make_token(two_orgs.a.profile(role).user_id))
    assert resp.status_code == 403
    _assert_error_shape(resp)


@pytest.mark.parametrize(("route", "role"), ALLOWED, ids=_pair_ids(ALLOWED))
def test_allowed_role_is_not_denied(
    client: TestClient, two_orgs: TwoOrgs, route: Route, role: str
) -> None:
    resp = _call(client, route, make_token(two_orgs.a.profile(role).user_id))
    assert resp.status_code not in (401, 403)


@pytest.mark.parametrize("route", [r for r in ROUTES if r.cross_org_404], ids=_id)
def test_cross_org_id_is_404(client: TestClient, two_orgs: TwoOrgs, route: Route) -> None:
    assert route.cross_org_path, "cross_org_404 rows need cross_org_path"
    role = (route.roles or ALL_ROLES)[0]
    token = make_token(two_orgs.b.profile(role).user_id)
    resp = client.request(
        route.method, route.cross_org_path, headers=_bearer(token), json=route.body
    )
    assert resp.status_code == 404
    _assert_error_shape(resp)


def test_each_admin_gets_only_their_own_join_code(client: TestClient, two_orgs: TwoOrgs) -> None:
    a = client.get(f"{PREFIX}/org/join-code", headers=_bearer(make_token(two_orgs.a.admin.user_id)))
    b = client.get(f"{PREFIX}/org/join-code", headers=_bearer(make_token(two_orgs.b.admin.user_id)))
    assert a.status_code == b.status_code == 200
    assert _digits(a.json()["join_code"]) == two_orgs.a.join_code
    assert _digits(b.json()["join_code"]) == two_orgs.b.join_code


def test_join_code_ignores_org_id_in_query(client: TestClient, two_orgs: TwoOrgs) -> None:
    resp = client.get(
        f"{PREFIX}/org/join-code",
        params={"org_id": str(two_orgs.a.org_id)},
        headers=_bearer(make_token(two_orgs.b.admin.user_id)),
    )
    assert resp.status_code == 200
    assert _digits(resp.json()["join_code"]) == two_orgs.b.join_code


def test_join_code_ignores_org_id_in_body_and_header(client: TestClient, two_orgs: TwoOrgs) -> None:
    resp = client.request(
        "GET",
        f"{PREFIX}/org/join-code",
        json={"org_id": str(two_orgs.a.org_id)},
        headers={
            **_bearer(make_token(two_orgs.b.admin.user_id)),
            "X-Org-Id": str(two_orgs.a.org_id),
        },
    )
    assert resp.status_code == 200
    assert _digits(resp.json()["join_code"]) == two_orgs.b.join_code


def test_me_reports_token_owners_org_not_a_requested_one(
    client: TestClient, two_orgs: TwoOrgs
) -> None:
    resp = client.get(
        f"{PREFIX}/me",
        params={"org_id": str(two_orgs.a.org_id)},
        headers=_bearer(make_token(two_orgs.b.agent.user_id)),
    )
    assert resp.status_code == 200
    assert resp.json()["org_id"] == str(two_orgs.b.org_id)
    assert resp.json()["role"] == "agent"


@pytest.mark.parametrize("field", ["org_id", "role"])
def test_onboarding_rejects_org_id_and_role_in_body(
    client: TestClient, two_orgs: TwoOrgs, db: FakeDb, field: str
) -> None:
    uid = uuid.uuid4()
    value = str(two_orgs.a.org_id) if field == "org_id" else "admin"
    resp = client.post(
        f"{PREFIX}/onboarding",
        json={"join_code": two_orgs.b.join_code, field: value},
        headers=_bearer(make_token(uid)),
    )
    assert resp.status_code == 422
    _assert_error_shape(resp)
    assert uid not in db.profiles


def test_onboarding_ignores_org_id_in_query(client: TestClient, two_orgs: TwoOrgs) -> None:
    resp = client.post(
        f"{PREFIX}/onboarding",
        params={"org_id": str(two_orgs.a.org_id), "role": "admin"},
        json={"join_code": two_orgs.b.join_code},
        headers=_bearer(make_token(uuid.uuid4())),
    )
    assert resp.status_code == 201
    assert resp.json()["org_id"] == str(two_orgs.b.org_id)
    assert resp.json()["role"] != "admin"
