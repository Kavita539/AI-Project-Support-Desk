from uuid import UUID

from psycopg import Connection

# Both bootstrapping queries below take no org_id: they exist to create an org or to
# find the org a join code belongs to, so there is no org to scope by yet.


def insert_org(conn: Connection[dict[str, object]], name: str) -> UUID:
    row = conn.execute("insert into orgs (name) values (%s) returning id", (name,)).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def insert_join_code(conn: Connection[dict[str, object]], org_id: UUID, code: str) -> bool:
    """Returns False if the code is already taken by another org, so the caller can retry."""
    cur = conn.execute(
        "insert into org_join_codes (org_id, code) values (%s, %s) on conflict (code) do nothing",
        (org_id, code),
    )
    return cur.rowcount == 1


def find_org_id_by_join_code(conn: Connection[dict[str, object]], code: str) -> UUID | None:
    row = conn.execute("select org_id from org_join_codes where code = %s", (code,)).fetchone()
    return None if row is None else UUID(str(row["org_id"]))


def get_join_code(conn: Connection[dict[str, object]], org_id: UUID) -> str | None:
    row = conn.execute(
        "select code from org_join_codes where org_id = %s", (org_id,)
    ).fetchone()
    return None if row is None else str(row["code"])
