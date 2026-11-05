import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from app.auth import get_key_resolver
from app.config import get_settings
from app.db.jobs import Job
from app.db.pool import get_conn
from app.db.queries.documents import Document
from app.db.queries.profiles import Profile
from app.errors import ApiError
from app.llm import clear_provider_cache
from app.main import app
from app.storage import StorageObjectNotFound, get_storage

SUPABASE_URL = "https://test.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"

_private_key = ec.generate_private_key(ec.SECP256R1())
_other_key = ec.generate_private_key(ec.SECP256R1())


def make_token(
    user_id: UUID,
    *,
    key: ec.EllipticCurvePrivateKey = _private_key,
    expires_in: int = 3600,
    drop: tuple[str, ...] = (),
    **overrides: Any,
) -> str:
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": int(time.time()) + expires_in,
        "email": "user@example.com",
    }
    claims.update(overrides)
    for name in drop:
        claims.pop(name)
    return jwt.encode(claims, key, algorithm="ES256", headers={"kid": "test"})


def make_forged_token(user_id: UUID) -> str:
    """Signed with a key Supabase never issued."""
    return make_token(user_id, key=_other_key)


class FakeConn:
    """Stands in for a psycopg connection. Real transactions are not simulated: atomicity
    is a property of Postgres and is checked against the live database, not here."""

    def transaction(self) -> AbstractContextManager[None]:
        return nullcontext()

    def commit(self) -> None:
        pass


class FakeDb:
    """Stands in for the database: profiles, orgs and join codes held in dicts."""

    def __init__(self) -> None:
        self.profiles: dict[UUID, Profile] = {}
        self.orgs: dict[UUID, str] = {}
        self.codes: dict[str, UUID] = {}  # code -> org_id
        self.reject_next_codes = 0  # simulate join-code collisions
        self.deleted_users: set[UUID] = set()  # valid tokens whose auth.users row is gone

    def add_profile(self, role: str = "agent", org_id: UUID | None = None) -> Profile:
        profile = Profile(uuid4(), org_id or uuid4(), role, f"{role}@example.com")
        self.profiles[profile.user_id] = profile
        return profile

    def add_org(self, name: str = "Acme", code: str = "K7M2QX9P") -> UUID:
        org_id = uuid4()
        self.orgs[org_id] = name
        self.codes[code] = org_id
        return org_id

    # Fakes for the functions in app.db.queries.orgs / profiles
    def insert_org(self, _conn: object, name: str) -> UUID:
        org_id = uuid4()
        self.orgs[org_id] = name
        return org_id

    def insert_join_code(self, _conn: object, org_id: UUID, code: str) -> bool:
        if self.reject_next_codes > 0:
            self.reject_next_codes -= 1
            return False
        if code in self.codes:
            return False
        self.codes[code] = org_id
        return True

    def find_org_id_by_join_code(self, _conn: object, code: str) -> UUID | None:
        return self.codes.get(code)

    def get_join_code(self, _conn: object, org_id: UUID) -> str | None:
        return next((c for c, o in self.codes.items() if o == org_id), None)

    def insert_profile(
        self, _conn: object, user_id: UUID, org_id: UUID, role: str, email: str
    ) -> Profile:
        if user_id in self.deleted_users:
            raise ForeignKeyViolation("profiles_user_id_fkey")
        if user_id in self.profiles:
            raise UniqueViolation("profiles_pkey")
        self.profiles[user_id] = Profile(user_id, org_id, role, email)
        return self.profiles[user_id]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDb:
    fake = FakeDb()
    monkeypatch.setattr("app.auth.get_profile", lambda _conn, uid: fake.profiles.get(uid))
    monkeypatch.setattr("app.db.queries.orgs.insert_org", fake.insert_org)
    monkeypatch.setattr("app.db.queries.orgs.insert_join_code", fake.insert_join_code)
    monkeypatch.setattr(
        "app.db.queries.orgs.find_org_id_by_join_code", fake.find_org_id_by_join_code
    )
    monkeypatch.setattr("app.db.queries.orgs.get_join_code", fake.get_join_code)
    monkeypatch.setattr("app.services.onboarding.insert_profile", fake.insert_profile)
    return fake


class FakeStorage:
    """In-memory stand-in for Supabase Storage."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.fail_upload = False
        self.deleted: list[str] = []

    def upload(self, path: str, data: bytes, content_type: str) -> None:
        if self.fail_upload:
            raise ApiError(502, "storage_error", "File storage is unavailable, please try again.")
        self.objects[path] = (data, content_type)

    def delete(self, path: str) -> None:
        self.deleted.append(path)
        self.objects.pop(path, None)

    def download(self, path: str) -> bytes:
        if path not in self.objects:
            raise StorageObjectNotFound(path)
        return self.objects[path][0]


@dataclass
class FakeJob:
    org_id: UUID
    type: str
    payload: dict[str, object]
    status: str = "queued"


class FakeDocs:
    """Stands in for app.db.queries.documents and the job functions in app.db.jobs."""

    def __init__(self) -> None:
        self.docs: dict[UUID, Document] = {}
        self.jobs: list[FakeJob] = []
        self.fail_insert = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in (
            "insert_document",
            "get_document",
            "get_document_for_update",
            "list_documents",
            "count_documents",
            "delete_document",
            "mark_queued",
        ):
            monkeypatch.setattr(f"app.db.queries.documents.{name}", getattr(self, name))
        monkeypatch.setattr("app.db.jobs.enqueue", self.enqueue)
        monkeypatch.setattr("app.db.jobs.delete_queued_ingest_jobs", self.delete_queued_ingest_jobs)

    def seed(
        self,
        org_id: UUID,
        *,
        name: str = "seed.txt",
        kind: str = "txt",
        status: str = "ready",
        error: str | None = None,
        storage_path: str | None | bool = True,
        created_at: datetime | None = None,
        doc_id: UUID | None = None,
    ) -> Document:
        """Add a document directly. storage_path True means a default path, None means none."""
        doc_id = doc_id or uuid4()
        path = f"{org_id}/{doc_id}/{name}" if storage_path is True else storage_path
        assert path is None or isinstance(path, str)
        doc = Document(
            doc_id, org_id, name, kind, path, status, error, None, 5, None,
            created_at or datetime.now(UTC),
        )  # fmt: skip
        self.docs[doc_id] = doc
        return doc

    def queued_jobs(self, doc_id: UUID) -> list[FakeJob]:
        return [j for j in self.jobs if j.payload.get("document_id") == str(doc_id)]

    def insert_document(
        self,
        _conn: object,
        org_id: UUID,
        doc_id: UUID,
        name: str,
        kind: str,
        storage_path: str | None,
        size_bytes: int | None,
        page_count: int | None,
        created_by: UUID | None,
    ) -> Document:
        if self.fail_insert:
            raise RuntimeError("db down")
        doc = Document(
            doc_id, org_id, name, kind, storage_path, "queued", None, page_count,
            size_bytes, created_by, datetime.now(UTC),
        )  # fmt: skip
        self.docs[doc_id] = doc
        return doc

    def get_document(self, _conn: object, org_id: UUID, doc_id: UUID) -> Document | None:
        doc = self.docs.get(doc_id)
        return doc if doc and doc.org_id == org_id else None

    get_document_for_update = get_document

    def list_documents(
        self, _conn: object, org_id: UUID, limit: int, offset: int
    ) -> list[Document]:
        mine = [d for d in self.docs.values() if d.org_id == org_id]
        mine.sort(key=lambda d: (d.created_at, str(d.id)), reverse=True)
        return mine[offset : offset + limit]

    def count_documents(self, _conn: object, org_id: UUID) -> int:
        return sum(1 for d in self.docs.values() if d.org_id == org_id)

    def delete_document(self, _conn: object, org_id: UUID, doc_id: UUID) -> bool:
        if self.get_document(None, org_id, doc_id) is None:
            return False
        del self.docs[doc_id]
        return True

    def mark_queued(self, _conn: object, org_id: UUID, doc_id: UUID) -> bool:
        doc = self.get_document(None, org_id, doc_id)
        if doc is None:
            return False
        self.docs[doc_id] = replace(doc, status="queued", error=None)
        return True

    def enqueue(
        self,
        _conn: object,
        org_id: UUID,
        job_type: str,
        payload: dict[str, object],
        run_after: object = None,
    ) -> Job:
        self.jobs.append(FakeJob(org_id, job_type, payload))
        now = datetime.now(UTC)
        return Job(uuid4(), org_id, job_type, payload, "queued", 0, 3, now, None, None, now)

    def delete_queued_ingest_jobs(self, _conn: object, org_id: UUID, doc_id: UUID) -> int:
        def doomed(j: FakeJob) -> bool:
            return (
                j.org_id == org_id
                and j.type == "ingest_document"
                and j.status == "queued"
                and j.payload.get("document_id") == str(doc_id)
            )

        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if not doomed(j)]
        return before - len(self.jobs)


@pytest.fixture
def docs(monkeypatch: pytest.MonkeyPatch) -> FakeDocs:
    fake = FakeDocs()
    fake.install(monkeypatch)
    return fake


@pytest.fixture(autouse=True)
def no_background_worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A TestClient lifespan must never start the ingestion thread."""
    monkeypatch.setenv("WORKER_ENABLED", "false")
    monkeypatch.setenv("LLM_PROVIDER", "fake")  # tests never reach a real LLM
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)  # a developer's real key must not leak in
    get_settings.cache_clear()
    clear_provider_cache()
    yield
    get_settings.cache_clear()
    clear_provider_cache()


@pytest.fixture
def storage(client: TestClient) -> FakeStorage:
    fake = FakeStorage()
    app.dependency_overrides[get_storage] = lambda: fake
    return fake


@dataclass(frozen=True)
class OrgFixture:
    """One org with a profile for each role, plus its join code."""

    org_id: UUID
    join_code: str
    admin: Profile
    agent: Profile
    customer: Profile

    def profile(self, role: str) -> Profile:
        return {"admin": self.admin, "agent": self.agent, "customer": self.customer}[role]


@dataclass(frozen=True)
class TwoOrgs:
    a: OrgFixture
    b: OrgFixture


@pytest.fixture
def two_orgs(db: FakeDb) -> TwoOrgs:
    """Org A and org B, each with an admin, an agent and a customer, and distinct join codes."""

    def build(name: str, code: str) -> OrgFixture:
        org_id = db.add_org(name, code)
        return OrgFixture(
            org_id=org_id,
            join_code=code,
            admin=db.add_profile("admin", org_id),
            agent=db.add_profile("agent", org_id),
            customer=db.add_profile("customer", org_id),
        )

    return TwoOrgs(a=build("Org A", "AAAA2222"), b=build("Org B", "BBBB3333"))


@pytest.fixture
def raw_client(db: FakeDb, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Like client, but get_conn is NOT faked, so the real (closed) pool behavior shows."""
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    get_settings.cache_clear()
    app.dependency_overrides[get_key_resolver] = lambda: lambda _token: _private_key.public_key()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


@pytest.fixture
def client(db: FakeDb, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("DATABASE_URL", "")  # never open a real pool in tests
    get_settings.cache_clear()
    app.dependency_overrides[get_key_resolver] = lambda: lambda _token: _private_key.public_key()
    app.dependency_overrides[get_conn] = lambda: FakeConn()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
