import secrets
from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection
from psycopg.errors import UniqueViolation

from app.auth import Identity
from app.db.queries import orgs
from app.db.queries.profiles import Profile, insert_profile
from app.errors import ApiError

# No 0/O or 1/I, so codes survive being read aloud or copied from a screenshot.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8


@dataclass(frozen=True)
class Onboarded:
    profile: Profile
    join_code: str | None  # only set when the caller just created the org


def new_join_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))


def normalize_join_code(raw: str) -> str:
    """Users type codes as shown (K7M2-QX9P); we store and compare them without the dash."""
    return "".join(ch for ch in raw.upper() if ch.isalnum())


def format_join_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _already_onboarded() -> ApiError:
    return ApiError(409, "already_onboarded", "This account already belongs to an org.")


def create_org(
    conn: Connection[dict[str, object]], who: Identity, email: str, name: str
) -> Onboarded:
    """The caller founds a new org and becomes its Admin. All or nothing."""
    try:
        with conn.transaction():
            org_id = orgs.insert_org(conn, name)
            profile = insert_profile(conn, who.user_id, org_id, "admin", email)
            for _ in range(5):
                code = new_join_code()
                if orgs.insert_join_code(conn, org_id, code):
                    return Onboarded(profile, format_join_code(code))
            raise ApiError(503, "try_again", "Could not create the org, please retry.")
    except UniqueViolation:
        raise _already_onboarded() from None


def join_org(
    conn: Connection[dict[str, object]], who: Identity, email: str, join_code: str
) -> Onboarded:
    """The caller joins the org the code belongs to, always as a Customer."""
    org_id = orgs.find_org_id_by_join_code(conn, normalize_join_code(join_code))
    if org_id is None:
        raise ApiError(404, "invalid_join_code", "That join code is not valid.")
    try:
        with conn.transaction():
            profile = insert_profile(conn, who.user_id, org_id, "customer", email)
    except UniqueViolation:
        raise _already_onboarded() from None
    return Onboarded(profile, None)


def get_org_join_code(conn: Connection[dict[str, object]], org_id: UUID) -> str:
    code = orgs.get_join_code(conn, org_id)
    if code is None:
        raise ApiError(404, "not_found", "This org has no join code.")
    return format_join_code(code)
