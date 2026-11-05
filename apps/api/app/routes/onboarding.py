from typing import Annotated, Self

from fastapi import APIRouter, Depends
from psycopg import Connection
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.auth import Identity, get_identity
from app.db.pool import get_conn
from app.errors import ApiError
from app.routes.me import MeResponse
from app.services import onboarding

router = APIRouter()

OrgName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
JoinCode = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]


class OnboardingRequest(BaseModel):
    # extra="forbid": a body carrying org_id or role is rejected, never silently used.
    model_config = ConfigDict(extra="forbid")

    org_name: OrgName | None = None
    join_code: JoinCode | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.org_name is None) == (self.join_code is None):
            raise ValueError("Send exactly one of org_name or join_code.")
        return self


class OnboardingResponse(MeResponse):
    join_code: str | None = Field(default=None, description="Only when you created the org")


@router.post("/onboarding", status_code=201)
def onboard(
    body: OnboardingRequest,
    who: Annotated[Identity, Depends(get_identity)],
    conn: Annotated[Connection[dict[str, object]], Depends(get_conn)],
) -> OnboardingResponse:
    if who.email is None:
        raise ApiError(400, "email_required", "Your login has no email address.")
    if body.org_name is not None:
        result = onboarding.create_org(conn, who, who.email, body.org_name)
    else:
        assert body.join_code is not None
        result = onboarding.join_org(conn, who, who.email, body.join_code)
    p = result.profile
    return OnboardingResponse(
        user_id=str(p.user_id),
        org_id=str(p.org_id),
        role=p.role,
        email=p.email,
        join_code=result.join_code,
    )
