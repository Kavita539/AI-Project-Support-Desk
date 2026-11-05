from dataclasses import dataclass
from uuid import UUID

from psycopg import Connection


@dataclass(frozen=True)
class Profile:
    user_id: UUID
    org_id: UUID
    role: str
    email: str


def insert_profile(
    conn: Connection[dict[str, object]], user_id: UUID, org_id: UUID, role: str, email: str
) -> Profile:
    """Raises psycopg.errors.UniqueViolation if the user already has a profile."""
    conn.execute(
        "insert into profiles (user_id, org_id, role, email) values (%s, %s, %s, %s)",
        (user_id, org_id, role, email),
    )
    return Profile(user_id, org_id, role, email)


def get_profile(conn: Connection[dict[str, object]], user_id: UUID) -> Profile | None:
    """Look up who a verified user is. This is the one query that takes no org_id,
    because its whole job is to find the org_id the user belongs to."""
    row = conn.execute(
        "select user_id, org_id, role, email from profiles where user_id = %s",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return Profile(
        user_id=UUID(str(row["user_id"])),
        org_id=UUID(str(row["org_id"])),
        role=str(row["role"]),
        email=str(row["email"]),
    )
