from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db.pool import close_pool, open_pool
from app.errors import install_error_handlers
from app.routes import me


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().database_url:
        open_pool()
    yield
    close_pool()


app = FastAPI(title="AI Project Support Desk", lifespan=lifespan)
install_error_handlers(app)

# The browser app runs on a different origin, so it must be allowed to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_headers=["Authorization", "Content-Type"],
    allow_methods=["*"],
)

app.include_router(me.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
