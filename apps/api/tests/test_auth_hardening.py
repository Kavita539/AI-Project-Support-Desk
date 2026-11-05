import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.auth import get_key_resolver
from app.config import Settings
from app.db.pool import get_conn
from app.db.queries.profiles import get_profile
from app.main import app
from tests.conftest import ISSUER, FakeDb, make_token

SECRET = "x" * 40


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _claims(user_id: object) -> dict[str, Any]:
    return {
        "sub": str(user_id),
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": int(time.time()) + 60,
    }


def test_401_has_www_authenticate_header_and_error_body(client: TestClient) -> None:
    res = client.get("/api/v1/me", headers=auth("garbage"))
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"
    assert res.json()["error"]["code"] == "unauthorized"


def test_hs256_token_is_rejected(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    token = jwt.encode(_claims(profile.user_id), SECRET, algorithm="HS256")
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_alg_none_token_is_rejected(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    token = jwt.encode(_claims(profile.user_id), None, algorithm="none")  # type: ignore[arg-type]
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


@pytest.mark.parametrize("missing", ["exp", "sub"])
def test_token_missing_required_claim_is_401(client: TestClient, db: FakeDb, missing: str) -> None:
    profile = db.add_profile()
    token = make_token(profile.user_id, drop=(missing,))
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_non_uuid_sub_is_401(client: TestClient) -> None:
    token = make_token(uuid4(), sub="not-a-uuid")
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_wrong_auth_scheme_is_401(client: TestClient) -> None:
    res = client.get("/api/v1/me", headers={"Authorization": "Basic abc123"})
    assert res.status_code == 401


def test_forged_org_and_role_claims_are_ignored(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile("customer")
    token = make_token(profile.user_id, org_id=str(uuid4()), role="admin")
    body = client.get("/api/v1/me", headers=auth(token)).json()
    assert body["org_id"] == str(profile.org_id)
    assert body["role"] == "customer"


def test_bad_token_never_borrows_a_db_connection(client: TestClient) -> None:
    borrowed: list[int] = []

    def counting_conn() -> Iterator[None]:
        borrowed.append(1)
        yield None

    app.dependency_overrides[get_conn] = counting_conn
    assert client.get("/api/v1/me", headers=auth("garbage")).status_code == 401
    assert client.get("/api/v1/me").status_code == 401
    assert borrowed == []


def test_jwks_outage_is_503_not_401(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()

    def broken_resolver() -> Any:
        def resolve(_token: str) -> object:
            raise jwt.PyJWKClientConnectionError("jwks unreachable")

        return resolve

    app.dependency_overrides[get_key_resolver] = broken_resolver
    res = client.get("/api/v1/me", headers=auth(make_token(profile.user_id)))
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "auth_unavailable"


def test_real_key_resolver_uses_the_jwks_client(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_key = object()
    fake_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=fake_key)
    )
    monkeypatch.setattr("app.auth._jwks_client", lambda: fake_client)
    assert get_key_resolver()("any.token.here") is fake_key


def test_no_db_pool_gives_503_in_error_shape_for_valid_token(
    raw_client: TestClient, db: FakeDb
) -> None:
    profile = db.add_profile()
    res = raw_client.get("/api/v1/me", headers=auth(make_token(profile.user_id)))
    assert res.status_code == 503
    assert res.json()["error"] == {
        "code": "db_unavailable",
        "message": "Database is unavailable.",
    }


def test_no_db_pool_still_gives_401_without_token(raw_client: TestClient) -> None:
    assert raw_client.get("/api/v1/me").status_code == 401


def test_unexpected_exception_returns_error_shape(raw_client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()

    def boom() -> Iterator[None]:
        raise ValueError("secret internals")
        yield None

    app.dependency_overrides[get_conn] = boom
    res = raw_client.get("/api/v1/me", headers=auth(make_token(profile.user_id)))
    assert res.status_code == 500
    assert res.json() == {"error": {"code": "internal_error", "message": "Something went wrong."}}


def test_cors_preflight_allows_the_web_origin(client: TestClient) -> None:
    res = client.options(
        "/api/v1/me",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_preflight_rejects_unknown_origin(client: TestClient) -> None:
    res = client.options(
        "/api/v1/me",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in res.headers


def test_get_profile_maps_the_row() -> None:
    uid, org = uuid4(), uuid4()
    row = {"user_id": uid, "org_id": org, "role": "admin", "email": "a@example.com"}
    conn = SimpleNamespace(execute=lambda *_a: SimpleNamespace(fetchone=lambda: row))
    profile = get_profile(cast(Any, conn), uid)
    assert profile is not None
    assert (profile.user_id, profile.org_id, profile.role) == (uid, org, "admin")


def test_get_profile_returns_none_when_no_row() -> None:
    conn = SimpleNamespace(execute=lambda *_a: SimpleNamespace(fetchone=lambda: None))
    assert get_profile(cast(Any, conn), uuid4()) is None


def test_settings_missing_required_and_cors_parsing() -> None:
    empty = Settings(_env_file=None)  # type: ignore[call-arg]
    assert empty.missing_required() == ["DATABASE_URL", "SUPABASE_URL"]
    full = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql://x",
        supabase_url="https://p.supabase.co",
        cors_origins="https://a.vercel.app, http://localhost:5173,",
    )
    assert full.missing_required() == []
    assert full.cors_origin_list == ["https://a.vercel.app", "http://localhost:5173"]


# --- clock skew (found by the live end-to-end check: this machine ran ~109s behind) ---


def test_token_issued_in_the_future_is_accepted(client: TestClient, db: FakeDb) -> None:
    """Our clock behind Supabase's makes fresh tokens look issued in the future."""
    profile = db.add_profile()
    token = make_token(profile.user_id, iat=int(time.time()) + 120)
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200


def test_token_expired_within_the_skew_allowance_is_accepted(
    client: TestClient, db: FakeDb
) -> None:
    profile = db.add_profile()
    token = make_token(profile.user_id, expires_in=-30)
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 200


def test_token_expired_beyond_the_skew_allowance_is_rejected(
    client: TestClient, db: FakeDb
) -> None:
    profile = db.add_profile()
    token = make_token(profile.user_id, expires_in=-300)
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401
