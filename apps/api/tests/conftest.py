import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.auth import get_key_resolver
from app.config import get_settings
from app.db.pool import get_conn
from app.db.queries.profiles import Profile
from app.main import app

SUPABASE_URL = "https://test.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"

_private_key = ec.generate_private_key(ec.SECP256R1())
_other_key = ec.generate_private_key(ec.SECP256R1())


def make_token(
    user_id: UUID,
    *,
    key: ec.EllipticCurvePrivateKey = _private_key,
    expires_in: int = 3600,
    **overrides: Any,
) -> str:
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": int(time.time()) + expires_in,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "test"})


def make_forged_token(user_id: UUID) -> str:
    """Signed with a key Supabase never issued."""
    return make_token(user_id, key=_other_key)


class FakeDb:
    """Stands in for the database: a dict of user_id -> Profile."""

    def __init__(self) -> None:
        self.profiles: dict[UUID, Profile] = {}

    def add_profile(self, role: str = "agent") -> Profile:
        profile = Profile(uuid4(), uuid4(), role, f"{role}@example.com")
        self.profiles[profile.user_id] = profile
        return profile


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr("app.auth.get_profile", lambda _conn, uid: fake.profiles.get(uid))
    return fake


@pytest.fixture
def client(db: FakeDb, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("DATABASE_URL", "")  # never open a real pool in tests
    get_settings.cache_clear()
    app.dependency_overrides[get_key_resolver] = lambda: lambda _token: _private_key.public_key()
    app.dependency_overrides[get_conn] = lambda: None
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
