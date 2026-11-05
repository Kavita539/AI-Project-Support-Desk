from fastapi import APIRouter
from pydantic import BaseModel

from app.auth import CurrentUser

router = APIRouter()


class MeResponse(BaseModel):
    user_id: str
    org_id: str
    role: str
    email: str


@router.get("/me")
def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        user_id=str(user.user_id),
        org_id=str(user.org_id),
        role=user.role,
        email=user.email,
    )
