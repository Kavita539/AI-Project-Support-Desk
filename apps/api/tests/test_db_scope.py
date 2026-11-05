from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db.pool import Conn, get_conn


def test_connection_is_released_before_the_response_is_sent() -> None:
    """A write must be committed before the client sees the response. With the default
    dependency scope FastAPI releases the connection AFTER sending, so a quick follow-up
    request can miss the row. This fails if Conn loses scope="function"."""
    events: list[str] = []

    def fake_conn() -> Iterator[None]:
        yield None
        events.append("connection released")

    mini = FastAPI()

    @mini.get("/x")
    def route(_conn: Conn) -> dict[str, str]:
        return {"ok": "yes"}

    mini.dependency_overrides[get_conn] = fake_conn

    class Spy:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            async def spying_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    events.append("response sent")
                await send(message)

            await self.app(scope, receive, spying_send)

    assert TestClient(Spy(mini)).get("/x").status_code == 200
    assert events == ["connection released", "response sent"]
