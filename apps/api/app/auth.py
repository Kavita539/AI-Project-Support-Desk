import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from app.config import get_settings
from app.db.pool import Conn
from app.db.queries.profiles import Profile, get_profile
from app.errors import ApiError

logger = logging.getLogger(__name__)

# Turns a token into the public key that should have signed it.
KeyResolver = Callable[[str], object]

# Supabase signs with an asymmetric key. We never accept HS256 or "none", so nobody
# can forge a token by claiming a weaker algorithm.
_ALGORITHMS = ["ES256", "RS256"]

_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _jwks_client() -> PyJWKClient:
    # Fetches Supabase's public keys once and caches them.
    return PyJWKClient(get_settings().jwks_url, cache_keys=True)


def get_key_resolver() -> KeyResolver:
    """Dependency, so tests can swap in a local test key instead of calling Supabase."""
    return lambda token: _jwks_client().get_signing_key_from_jwt(token).key


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message)


@dataclass(frozen=True)
class Identity:
    """What a verified token proves: who the user is. Says nothing about org or role."""

    user_id: UUID
    email: str | None


def verify_token(token: str, resolve_key: KeyResolver) -> Identity:
    """Return the identity of a valid token (the `sub` and `email` claims), or raise 401."""
    try:
        key = resolve_key(token)
        claims = jwt.decode(
            token,
            key,  # type: ignore[arg-type]
            algorithms=_ALGORITHMS,
            audience="authenticated",
            issuer=get_settings().jwt_issuer,
            options={"require": ["exp", "sub"]},
        )
        email = claims.get("email")
        return Identity(UUID(str(claims["sub"])), email if isinstance(email, str) else None)
    except jwt.PyJWKClientConnectionError:
        # Our problem (Supabase unreachable or SUPABASE_URL wrong), not the user's token.
        logger.exception("Could not fetch Supabase signing keys")
        raise ApiError(503, "auth_unavailable", "Authentication service is unavailable.") from None
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized("Invalid or expired token.") from None


def get_identity(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    resolve_key: Annotated[KeyResolver, Depends(get_key_resolver)],
) -> Identity:
    """Step 1: prove who is calling. Runs before any database connection is borrowed,
    so requests with bad tokens never touch the pool."""
    if creds is None:
        raise _unauthorized("Missing bearer token.")
    return verify_token(creds.credentials, resolve_key)


def get_current_user(
    identity: Annotated[Identity, Depends(get_identity)],
    conn: Conn,
) -> Profile:
    """Step 2: the only place org_id and role come from: the profiles row."""
    profile = get_profile(conn, identity.user_id)
    if profile is None:
        raise ApiError(403, "no_profile", "This account has no profile yet.")
    return profile


CurrentUser = Annotated[Profile, Depends(get_current_user)]


def require_role(*roles: str) -> Callable[[Profile], Profile]:
    """Dependency factory: allow only the given roles, otherwise 403."""

    def check(user: CurrentUser) -> Profile:
        if user.role not in roles:
            raise ApiError(403, "forbidden", "Your role cannot do this.")
        return user

    return check
