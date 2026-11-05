# Session handoff

Read this first in a new session, then `CLAUDE.md` and `docs/plan.md`. No secrets belong in this file. Update it at the end of every plan step, in the same branch as the code (see "Docs are part of done" in `CLAUDE.md`).

Last updated: 2026-11-05, end of step 6 on branch `feat/upload-jobs` (committed, not pushed, not merged). `main` is at `8741ef4` (step 5 merged as PR #5).

## Where we are

- Plan steps 1 to 6 are done. Steps 1 to 5 are merged (PRs #1 to #5); step 6 is on `feat/upload-jobs` awaiting push and PR. **Step 7 (ingestion worker) is next**; branch from `main` only after step 6 is merged.
- API endpoints that exist: `GET /api/v1/health`, `GET /me` (read-only), `POST /onboarding`, `GET /org/join-code` (admin), `POST /documents` and `GET /documents/{id}` (admin). Nothing for ask or tickets yet.
- `ruff` and `mypy` are clean; `pytest` gives 154 passed, 29 skipped from `apps/api`. All 29 skips are integration tests (14 onboarding, 4 documents, 11 jobs) because `TEST_DATABASE_URL` is unset locally. `test_cross_org_id_is_404` now runs 1 case.
- **The 29 integration tests only run in CI.** There is no Docker or Postgres on this machine. Check the CI log for the 15 added in step 6. The jobs SQL (`claim_next`, including `SKIP LOCKED` with two connections) was also run once against the live database on 2026-11-05 (rolled back, see the live check below), so a CI failure there would point at the test fixtures rather than the SQL.
- The private Storage bucket `documents` (10 MB, pdf/markdown/text, no `storage.objects` policies) exists on the live project: migration `20261105000003_documents_bucket` was applied on 2026-11-05. The bucket name is the `STORAGE_BUCKET` setting.
- Supabase project `support-desk` (production, free plan, region ap-south-1). Project ref: `yiakeyeohwpbjjseugha`. The older paused project `task-manager` in the same org is unrelated; do not use it.
- Migrations applied to it (all five are in `supabase/migrations/`): `20261028000001_schema`, `20261028000002_rls`, `20261105000001_org_join_codes`, `20261105000002_org_join_codes_hardening`, `20261105000003_documents_bucket`.
- Live end-to-end checks (real API on a local port, Supabase Auth, real database and Storage) passed on 2026-11-05 and left nothing behind (verified: 0 objects, documents, jobs, orgs and users afterwards): onboarding 30/30, and upload 30/30 (admin upload of .md and .pdf, 401/403/404 and 415/413/400 cases, an unauthenticated 12 MB body refused with 413, org-scoped storage paths, the object present in the private bucket and unreadable without the service key, two ingest jobs claimed on separate connections with `SKIP LOCKED` and rolled back). Both scripts were throwaways in a scratchpad and are **not in the repo**. The upload check signed users in with the service-role key as `apikey` because the anon key is not in `apps/api/.env`, so "unreadable by other roles" was tested with no credentials and another org's user token, not with the anon key.

## What the API code looks like

```
apps/api/app/
  config.py         Settings from env (DATABASE_URL, SUPABASE_URL, CORS_ORIGINS, ...). Boot fails if DATABASE_URL or SUPABASE_URL is empty
  auth.py           verify_token -> Identity(user_id, email); get_identity; get_current_user (profiles row); require_role(...)
  errors.py         ApiError + handlers: every error is {"error": {"code", "message"}}, incl. 404, 422 and unexpected 500
  db/pool.py        psycopg pool; `Conn` = the connection dependency to use in routes (see decisions)
  db/queries/       profiles.py, orgs.py, documents.py (raw SQL)
  db/jobs.py        enqueue, claim_next (SKIP LOCKED), complete, fail, requeue_stale. claim_next and requeue_stale are global on purpose
  storage.py        Storage protocol + SupabaseStorage (httpx, service-role key); tests use FakeStorage
  services/         onboarding.py (create_org, join_org, join-code helpers), documents.py (upload validation + orchestration)
  routes/           me.py, onboarding.py, org.py, documents.py
  main.py           also holds a middleware that refuses oversized or length-less POST /documents bodies before parsing
apps/api/tests/     conftest.py has FakeDb / FakeConn, make_token() and the `two_orgs` fixture (orgs A and B, each with admin, agent, customer)
                    endpoint_matrix.py: one row per /api/v1 route; test_tenancy.py: completeness + 401/403/org_id-from-client tests
                    test_me, test_onboarding, test_documents_upload, test_storage, test_auth_hardening, test_db_scope, test_health
  integration/      real-Postgres tests (skipped unless TEST_DATABASE_URL is set): supabase_stubs.sql, conftest.py, test_onboarding_sql.py, test_documents_sql.py, test_jobs_sql.py
```

## Environment

- `apps/api/.env` exists locally and is filled in (gitignored). `SUPABASE_JWT_SECRET` is **not used**: this project signs tokens with ES256 and the API verifies them against the public JWKS. `LLM_PROVIDER=fake` until step 9.
- The web `.env.local` does not exist yet (`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`).
- Windows machine. Port 8000 is used by something else locally: run the API on another port (for example `--port 8765`).
- **The local clock is about 109 seconds behind real time** (checked 2026-11-05): the Windows Time service is Disabled. The API tolerates 60 seconds of skew, so real tokens can still fail locally until the clock is fixed. Fix in an administrator PowerShell: `Set-Service w32time -StartupType Automatic; Start-Service w32time; w32tm /resync /force`.
- No `gh` CLI installed; pull requests are opened in the browser.

## Stashed work: Ask page UI

The customer Ask page was built first against a mock API, then stashed so branches stay backend-focused. It is still there: `git stash list` shows `stash@{0}: Ask page UI with mock API`.

- Restore: `git stash pop`, then `npm install` in `apps/web`.
- Contents: `AskPage`, `TurnView`, `CitationList`, `Loading`, `DemoBanner`, `lib/api.ts` (mock, swap for real `/api/v1` calls later), `lib/types.ts`, `index.css`, two page tests, and the `@testing-library/user-event` dev dependency. It passed lint, typecheck, vitest, build and the Playwright smoke test when stashed.
- Not built yet: login, onboarding screen (create org / join with code), role routing, documents page, tickets, review UI.

## Decisions made

- **Process, every feature:** fetch, check the previous branch is merged into `origin/main`, fast-forward `main`, new `feat/<name>` branch. Run ruff, mypy and pytest before committing. Push and apply migrations only when the user says so. Docs are updated in the same branch.
- **Migrations** go through the Supabase CLI (`npx supabase db push --linked`, preview with `--dry-run`), never pasted in the dashboard. In agent mode the CLI needs `--yes` (and `--read-only` for queries); use `--yes` only after the user approves that migration.
- **Auth:** ES256/RS256 only (never HS256 or `none`), `aud=authenticated`, `iss=<SUPABASE_URL>/auth/v1`, `exp` and `sub` required, 60 s leeway, the `iat` check is skipped (it broke on clock skew). A key-server outage returns 503, a bad token 401. `org_id` and `role` come only from the `profiles` row.
- **Onboarding (option C):** `/me` is read-only (403 `no_profile` for a new user). `POST /onboarding` takes exactly one of `org_name` (caller becomes Admin and gets the join code) or `join_code` (caller becomes Customer). Unknown body fields such as `org_id` or `role` are rejected. Admins promote staff with `PATCH /org/users/{id}` (not built yet).
- **Join codes** live in `org_join_codes` (RLS on, no policies, no grants to `anon` or `authenticated`), not on `orgs`, because customers can read their own org row through RLS.
- **DB connections:** always take `conn: Conn` from `app/db/pool.py`, never `Depends(get_conn)` directly. `Conn` is function-scoped so a write commits before the response is sent; the default scope would commit after the client has the 201. Inside a service, run a read that precedes a write inside the same `with conn.transaction():`, otherwise psycopg uses a savepoint and nothing commits until later.
- **Upload (step 6):** through the API, admin only, `.pdf/.md/.txt`, 10 MB, 100 PDF pages. File goes to Storage first, then row + job commit together; a failed DB step deletes the file. The service calls `conn.commit()` itself because the auth lookup already opened the transaction (otherwise `conn.transaction()` is only a savepoint). Added `GET /documents/{id}` earlier than planned so the cross-org 404 test has an ID route.
- **Jobs:** `claim_next` is one UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1). It and `requeue_stale` are the only queries not scoped by `org_id`; the worker must use the claimed job's `org_id` afterwards. CLAUDE.md rule 1 now states this exception.
- UI-first on the Ask page was a deliberate deviation from plan order (step 12 before steps 4 to 11).
- The user is a front-end engineer learning backend: explain decisions in plain language, keep summaries short.

## Known gaps and risks

- Unit tests use in-memory fakes, which hide real SQL, transactions and constraints. Step 5 added real-Postgres tests for onboarding, but they run only in CI (see above) and cover only `create_org`, `join_org`, `get_profile` and the join-code table. Each later step that adds SQL should add integration tests too.
- The integration conftest DROPS the `public` and `auth` schemas. It refuses non-local hosts and any URL with a query string. Never point `TEST_DATABASE_URL` at Supabase or an SSH tunnel to it.
- Atomicity tests must leave the connection idle (`conn.commit()`) before calling the service and must not `conn.rollback()` afterwards, otherwise `conn.transaction()` is only a savepoint and the test cannot fail.
- The route-completeness test uses a FastAPI internal (`effective_route_contexts`) because `include_router` routes are lazy in this FastAPI version. If a FastAPI upgrade breaks it, fix `_registered_routes()` in `tests/test_tenancy.py`.
- Upload holds a pooled DB connection (max 5) while it parses the PDF and calls Storage (up to 30 s). Five slow concurrent uploads can make other requests return 503 `db_unavailable`. Fix later by releasing the connection before the Storage call.
- A strict `%PDF-` check at byte 0 rejects the rare valid PDF with junk before the header (415).
- The new integration tests clean the `jobs` and `documents` tables in a fixture that must run before `World.cleanup()` (those tables reference orgs without cascade).
- No rate limiting. Join-code guessing (2^40 codes) and unlimited org creation are deferred to step 16.
- Join codes cannot be rotated or expire. Orgs created by the future seed script need a code inserted (`GET /org/join-code` returns 404 for an org without one).
- CI has a Postgres service container (added in step 5) and was green on `529e8fe`.
- The Starlette test-client `httpx` deprecation warning is known and harmless.

## Step 7 starting notes

- Worker loop: call `jobs.claim_next`, then use the job's `org_id`. `complete` and `fail` have no fencing token: a slow worker whose job was re-queued by `requeue_stale` and re-claimed could finish the other worker's attempt. Match on `attempts` or `locked_at` when building the worker.
- `fail()` stores `last_error` untruncated; truncate before copying it into `documents.error` (that field is returned by `GET /documents/{id}`).
- Re-ingest must delete and insert chunks in one transaction, and the same explicit-`commit()` care applies.
- pypdf only counts pages at upload. Parsing hostile PDFs during ingestion could hang; consider a timeout.
- Every new route needs a matrix row in `tests/endpoint_matrix.py` (`cross_org_404=True` and `cross_org_path` for ID routes).
- `FakeDocs` lives in `tests/test_documents_upload.py` and is imported by `test_tenancy.py`; move it to `conftest.py` when convenient.
- Deferred from step 5: a rerunnable live end-to-end check script in `apps/api/scripts/` (needs the user's approval each run).

## Useful checks

- `npx supabase migration list --linked` shows local vs remote migrations.
- `npx supabase db query --linked --read-only "<sql>"` runs read-only SQL on the linked database.
- API from `apps/api`: `.venv\Scripts\ruff check .`, `.venv\Scripts\mypy`, `.venv\Scripts\pytest`.
