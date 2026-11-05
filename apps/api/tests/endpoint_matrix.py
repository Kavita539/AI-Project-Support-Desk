"""Every /api/v1 route and what it must do for each caller. test_tenancy.py checks it.

HOW A FUTURE STEP ADDS A ROW
    Add one Route(...) to ROUTES for each new route, in the same PR as the route. The
    completeness test fails until you do (and fails if a row has no real route).

    method, path   Exactly as FastAPI registers it, e.g. "/api/v1/tickets/{ticket_id}".
    public         True only for routes that need no token (health). Skips the 401 tests.
    roles          Roles allowed to call it; any other role must get 403. None means any
                   authenticated user with a profile (no 403 test).
    body           A valid JSON body for the happy path, if the route takes one.
    path_params    Values for {placeholders} when testing, e.g. {"ticket_id": "<uuid>"}.
    cross_org_404  For ID-based routes: True means a caller from org B asking for an org A
                   id must get 404 (never 403, hard rule 2). The test needs a seeded org A
                   row; extend the fixture in conftest.py to create one and name it in
                   `cross_org_path`. Leave False for routes with no ID in the path.
    cross_org_path The concrete org A path to request as org B when cross_org_404 is True.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    public: bool = False
    roles: tuple[str, ...] | None = None
    body: dict[str, Any] | None = None
    path_params: dict[str, str] = field(default_factory=dict)
    cross_org_404: bool = False
    cross_org_path: str | None = None
    # Extra keyword arguments for client.request (e.g. files= for multipart uploads).
    request_kwargs: dict[str, Any] = field(default_factory=dict)
    # False for routes a signed-in user without a profile row may call (onboarding).
    needs_profile: bool = True


# Fixed id of the org A document that the tenancy tests seed (see test_tenancy.py).
DOC_A_ID = "11111111-1111-4111-8111-111111111111"

ROUTES: list[Route] = [
    Route("GET", "/api/v1/health", public=True),
    Route("GET", "/api/v1/me"),
    # Any signed-in user may call it; a user who already has a profile gets 409 inside.
    Route("POST", "/api/v1/onboarding", body={"org_name": "Matrix Org"}, needs_profile=False),
    Route("GET", "/api/v1/org/join-code", roles=("admin",)),
    Route(
        "POST",
        "/api/v1/documents",
        roles=("admin",),
        request_kwargs={"files": {"file": ("matrix.txt", b"hello", "text/plain")}},
    ),
    Route(
        "GET",
        "/api/v1/documents/{document_id}",
        roles=("admin",),
        path_params={"document_id": DOC_A_ID},
        cross_org_404=True,
        cross_org_path=f"/api/v1/documents/{DOC_A_ID}",
    ),
]
