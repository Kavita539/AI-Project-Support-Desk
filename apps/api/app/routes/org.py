from typing import Annotated

from fastapi import APIRouter, Depends
from psycopg import Connection
from pydantic import BaseModel

from app.auth import require_role
from app.db.pool import get_conn
from app.db.queries.profiles import Profile
from app.services import onboarding

router = APIRouter()


class JoinCodeResponse(BaseModel):
    join_code: str


@router.get("/org/join-code")
def join_code(
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Annotated[Connection[dict[str, object]], Depends(get_conn)],
) -> JoinCodeResponse:
    return JoinCodeResponse(join_code=onboarding.get_org_join_code(conn, admin.org_id))
