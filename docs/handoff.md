# Session handoff

Read this first in a new session, then `CLAUDE.md` and `docs/plan.md`. No secrets belong in this file. Update it at the end of every plan step, in the same branch as the code (see "Docs are part of done" in `CLAUDE.md`).

Last updated: 2026-11-05, end of step 5 on branch `feat/tenancy-tests` (committed, not pushed, not yet merged). `main` is at `d603214`.

## Where we are

- Plan steps 1 to 5 are done. Steps 1 to 4b are merged (PRs #1 to #4); step 5 is on `feat/tenancy-tests` awaiting push and PR. **Step 6 (upload endpoint, jobs table) is next**; branch from `main` only after step 5 is merged.
- API endpoints that exist: `GET /api/v1/health`, `GET /me` (read-only), `POST /onboarding`, `GET /org/join-code` (admin). Nothing else yet: no documents, ask or tickets.
- `ruff` and `mypy` are clean; `pytest` gives 100 passed, 15 skipped from `apps/api`. The skips are 14 integration tests (no `TEST_DATABASE_URL` locally) and 1 empty parametrization (`test_cross_org_id_is_404`, no ID routes yet).
- **The 14 integration tests have never run.** There is no Docker or Postgres on this machine, so the first real run is CI after the push. If CI fails on them, suspect the stub SQL or an exception class first.
- Supabase project `support-desk` (production, free plan, region ap-south-1). Project ref: `yiakeyeohwpbjjseugha`. The older paused project `task-manager` in the same org is unrelated; do not use it.
- Migrations applied to it (all four are in `supabase/migrations/`): `20261028000001_schema`, `20261028000002_rls`, `20261105000001_org_join_codes`, `20261105000002_org_join_codes_hardening`.
- A live end-to-end check of onboarding (real API, Supabase Auth, real database) passed 30/30 on 2026-11-05 and left nothing behind. The script was a throwaway in a scratchpad and is **not in the repo**; see "Step 6 starting notes".

## What the API code looks like

```
apps/api/app/
  config.py         Settings from env (DATABASE_URL, SUPABASE_URL, CORS_ORIGINS, ...). Boot fails if DATABASE_URL or SUPABASE_URL is empty
  auth.py           verify_token -> Identity(user_id, email); get_identity; get_current_user (profiles row); require_role(...)
  errors.py         ApiError + handlers: every error is {"error": {"code", "message"}}, incl. 404, 422 and unexpected 500
  db/pool.py        psycopg pool; `Conn` = the connection dependency to use in routes (see decisions)
  db/queries/       profiles.py, orgs.py (raw SQL)
  services/         onboarding.py (create_org, join_org, join-code helpers)
  routes/           me.py, onboarding.py, org.py
apps/api/tests/     conftest.py has FakeDb / FakeConn, make_token() and the `two_orgs` fixture (orgs A and B, each with admin, agent, customer)
                    endpoint_matrix.py: one row per /api/v1 route; test_tenancy.py: completeness + 401/403/org_id-from-client tests
                    test_me, test_onboarding, test_auth_hardening, test_db_scope, test_health
  integration/      real-Postgres tests (skipped unless TEST_DATABASE_URL is set): supabase_stubs.sql, conftest.py, test_onboarding_sql.py
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
- UI-first on the Ask page was a deliberate deviation from plan order (step 12 before steps 4 to 11).
- The user is a front-end engineer learning backend: explain decisions in plain language, keep summaries short.

## Known gaps and risks

- Unit tests use in-memory fakes, which hide real SQL, transactions and constraints. Step 5 added real-Postgres tests for onboarding, but they are unrun (see above) and cover only `create_org`, `join_org`, `get_profile` and the join-code table. Each later step that adds SQL should add integration tests too.
- The integration conftest DROPS the `public` and `auth` schemas. It refuses non-local hosts and any URL with a query string. Never point `TEST_DATABASE_URL` at Supabase or an SSH tunnel to it.
- Atomicity tests must leave the connection idle (`conn.commit()`) before calling the service and must not `conn.rollback()` afterwards, otherwise `conn.transaction()` is only a savepoint and the test cannot fail.
- The route-completeness test uses a FastAPI internal (`effective_route_contexts`) because `include_router` routes are lazy in this FastAPI version. If a FastAPI upgrade breaks it, fix `_registered_routes()` in `tests/test_tenancy.py`.
- No rate limiting. Join-code guessing (2^40 codes) and unlimited org creation are deferred to step 16.
- Join codes cannot be rotated or expire. Orgs created by the future seed script need a code inserted (`GET /org/join-code` returns 404 for an org without one).
- CI now has a Postgres service container (added in step 5) but it has not run yet; it runs on the first push of `feat/tenancy-tests`.
- The Starlette test-client `httpx` deprecation warning is known and harmless.

## Step 6 starting notes

- Every new route needs a row in `tests/endpoint_matrix.py` or `test_tenancy.py` fails. ID-based routes (documents, tickets) must set `cross_org_404=True` and a `cross_org_path`, so the dormant cross-org 404 test finally runs; check that it does.
- New SQL should get integration tests in `tests/integration/`.
- Deferred from step 5 (user chose to skip): promote the live end-to-end check into a rerunnable script in `apps/api/scripts/` (temporary users and orgs tagged with a random marker, deleting only those). It touches the live project, so it needs the user's approval each run.

## Useful checks

- `npx supabase migration list --linked` shows local vs remote migrations.
- `npx supabase db query --linked --read-only "<sql>"` runs read-only SQL on the linked database.
- API from `apps/api`: `.venv\Scripts\ruff check .`, `.venv\Scripts\mypy`, `.venv\Scripts\pytest`.
