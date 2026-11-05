from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_role
from app.db.pool import Conn
from app.db.queries.profiles import Profile
from app.services import onboarding

router = APIRouter()


class JoinCodeResponse(BaseModel):
    join_code: str


@router.get("/org/join-code")
def join_code(
    admin: Annotated[Profile, Depends(require_role("admin"))],
    conn: Conn,
) -> JoinCodeResponse:
    return JoinCodeResponse(join_code=onboarding.get_org_join_code(conn, admin.org_id))
