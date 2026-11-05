import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db.pool import close_pool, open_pool
from app.errors import install_error_handlers
from app.routes import documents, me, onboarding, org
from app.services.documents import MAX_BYTES
from app.worker import build_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    missing = settings.missing_required()
    if missing:
        # Better to refuse to boot than to serve broken auth.
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    open_pool()
    worker = None
    if settings.worker_enabled:
        try:
            worker = build_worker()
        except Exception:
            # A bad provider setting must not take auth, upload and health down with it.
            logger.exception("Ingestion worker disabled: could not build it")
    if worker is not None:
        worker.start()
    try:
        yield
    finally:
        # Stop the worker first: it borrows connections from the pool we are about to close.
        # stop() joins the thread (up to 15 s), so keep it off the event loop.
        if worker is not None:
            await asyncio.to_thread(worker.stop)
        close_pool()


app = FastAPI(title="AI Project Support Desk", lifespan=lifespan)
install_error_handlers(app)

UPLOAD_PATH = "/api/v1/documents"
MAX_UPLOAD_BODY = MAX_BYTES + 1024 * 1024  # file limit plus multipart overhead
FAQ_PATH = "/api/v1/documents/faq"
MAX_FAQ_BODY = 16 * 1024  # 2700 chars at 4 bytes each is about 11 KB


@app.middleware("http")
async def limit_upload_body(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """FastAPI parses a multipart body before it checks the token, so an anonymous client could
    make us spool a huge body to disk. Refuse oversized or length-less uploads up front."""
    if request.method == "POST" and request.url.path == UPLOAD_PATH:
        raw = request.headers.get("content-length")
        if raw is None or not raw.isdigit():
            return _error(411, "length_required", "Content-Length is required for uploads.")
        if int(raw) > MAX_UPLOAD_BODY:
            return _error(413, "file_too_large", "The upload is too large. The limit is 10 MB.")
    elif request.method == "POST" and request.url.path == FAQ_PATH:
        # A FAQ body is two short strings, so a small cap keeps anonymous bodies cheap.
        raw = request.headers.get("content-length")
        if raw is not None and raw.isdigit() and int(raw) > MAX_FAQ_BODY:
            return _error(413, "body_too_large", "The request is too large.")
    return await call_next(request)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


# The browser app runs on a different origin, so it must be allowed to call us.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_headers=["Authorization", "Content-Type"],
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
)

app.include_router(me.router, prefix="/api/v1")
app.include_router(onboarding.router, prefix="/api/v1")
app.include_router(org.router, prefix="/api/v1")
app.include_router(documents.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
