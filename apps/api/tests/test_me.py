from uuid import uuid4

from fastapi.testclient import TestClient

from tests.conftest import FakeDb, make_forged_token, make_token


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def assert_error(res_json: dict[str, dict[str, str]], code: str) -> None:
    assert set(res_json) == {"error"}
    assert res_json["error"]["code"] == code
    assert res_json["error"]["message"]


def test_me_returns_profile_for_valid_token(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile("agent")
    res = client.get("/api/v1/me", headers=auth(make_token(profile.user_id)))
    assert res.status_code == 200
    assert res.json() == {
        "user_id": str(profile.user_id),
        "org_id": str(profile.org_id),
        "role": "agent",
        "email": "agent@example.com",
    }


def test_me_without_token_is_401(client: TestClient) -> None:
    res = client.get("/api/v1/me")
    assert res.status_code == 401
    assert_error(res.json(), "unauthorized")


def test_me_with_garbage_token_is_401(client: TestClient) -> None:
    res = client.get("/api/v1/me", headers=auth("not-a-jwt"))
    assert res.status_code == 401
    assert_error(res.json(), "unauthorized")


def test_me_with_expired_token_is_401(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    token = make_token(profile.user_id, expires_in=-300)
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_me_with_forged_signature_is_401(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    res = client.get("/api/v1/me", headers=auth(make_forged_token(profile.user_id)))
    assert res.status_code == 401


def test_me_with_wrong_audience_is_401(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    token = make_token(profile.user_id, aud="something-else")
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_me_with_wrong_issuer_is_401(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    token = make_token(profile.user_id, iss="https://evil.example.com/auth/v1")
    assert client.get("/api/v1/me", headers=auth(token)).status_code == 401


def test_me_ignores_org_id_in_query_string(client: TestClient, db: FakeDb) -> None:
    profile = db.add_profile()
    res = client.get(
        "/api/v1/me", params={"org_id": str(uuid4())}, headers=auth(make_token(profile.user_id))
    )
    assert res.json()["org_id"] == str(profile.org_id)


def test_me_with_valid_token_but_no_profile_is_403(client: TestClient) -> None:
    res = client.get("/api/v1/me", headers=auth(make_token(uuid4())))
    assert res.status_code == 403
    assert_error(res.json(), "no_profile")


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    res = client.get("/api/v1/nope")
    assert res.status_code == 404
    assert_error(res.json(), "not_found")
