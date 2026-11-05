# Session handoff

Read this first in a new session, then `CLAUDE.md` and `docs/plan.md`. No secrets belong in this file. Update it at the end of every plan step, in the same branch as the code (see "Docs are part of done" in `CLAUDE.md`).

Last updated: 2026-11-05, after step 9 was built on branch `feat/gemini-provider` (7 commits since `main` at `403c01d`; **not merged, not pushed**). Steps 1 to 8 are merged and live-checked.

## Where we are

- Plan steps 1 to 9 are done. Steps 1 to 8 are merged; step 9 (Gemini provider and `generate`) is on `feat/gemini-provider`, waiting for the user to push and merge. **Step 10 (retrieval, "I don't know" gate, `/ask`) is next**; branch from an up-to-date `main` once step 9 is merged. **Real Gemini has never been called**: step 9 was tested only with mocked HTTP transports and the fake provider.
- API endpoints that exist: `GET /api/v1/health`, `GET /me` (read-only), `POST /onboarding`, `GET /org/join-code` (admin), and for documents (all admin): `GET /documents` (paged), `POST /documents`, `POST /documents/faq`, `GET /documents/{id}`, `POST /documents/{id}/reprocess`, `DELETE /documents/{id}`. Nothing for ask or tickets yet. `GET /documents/{id}/source` is designed but not built.
- `ruff` and `mypy` are clean; `pytest` gives 537 passed, 49 skipped from `apps/api`. All 49 skips are integration tests (14 onboarding, 14 documents, 16 jobs, 5 chunks) because `TEST_DATABASE_URL` is unset locally.
- **The 49 integration tests have never run anywhere.** There is no Docker or Postgres on this machine. The SQL from steps 7 and 8 (`lock_running`, the attempts fence, `requeue_stale`, `replace_chunks`, the `mark_*` helpers, the `::vector` cast, and step 8's `list_documents`, `count_documents`, `get_document_for_update`, `delete_document`, `delete_queued_ingest_jobs`) was only read, not executed here. CI's Postgres service container must run them: check the CI log for failures in `test_documents_sql.py`, `test_jobs_sql.py` and `test_chunks_sql.py`. Nothing tests that `FOR UPDATE` actually blocks a second connection. Embeddings are the fake provider, so real Gemini vectors have not been stored anywhere. Step 9 added no migration (still the five below) and no dependency (`httpx` was already a runtime dependency).
- The private Storage bucket `documents` exists on the live project (migration `20261105000003_documents_bucket`, applied 2026-11-05). Steps 7 and 8 added **no migration**. The bucket only accepts pdf, markdown and text MIME types (a 400 for anything else); FAQ objects are `text/plain`, which the bucket accepted in the step 8 live check.
- Supabase project `support-desk` (production, free plan, region ap-south-1). Project ref: `yiakeyeohwpbjjseugha`. The older paused project `task-manager` in the same org is unrelated; do not use it.
- Migrations applied to it (all five are in `supabase/migrations/`): `20261028000001_schema`, `20261028000002_rls`, `20261105000001_org_join_codes`, `20261105000002_org_join_codes_hardening`, `20261105000003_documents_bucket`.
- Live end-to-end checks passed on 2026-11-05 and left nothing behind; the scripts were throwaways and are **not in the repo**: onboarding (30/30), upload (30/30), and the step 7 ingestion check (fake provider, random marker): a text PDF and a markdown file reached `ready` (3 chunks, all 768-dim vectors, all in the right org, page_count 3); a scanned PDF, a corrupt PDF and a missing Storage object each ended `failed` with the fixed readable sentence and no internals; re-ingesting the same PDF twice left 3 chunks; no job stuck. An independent read-only check afterwards found 0 orgs, documents, chunks, jobs and Storage objects. That check also showed Supabase answers a missing Storage object with HTTP 400 and a JSON body with `statusCode: "404"`; step 8 made `Storage.delete` tolerate it like `download` already did. Real Gemini embeddings were not tested (the provider exists since step 9, but the live check has not happened). **The step 8 live check PASSED on 2026-11-05** (26/26, fake provider, random marker, service layer called directly with a real DB and real Storage; no Auth user was created, so `created_by` was NULL): upload md and txt, create a FAQ (the `text/plain` object was accepted by the bucket); list (total, newest first, limit/offset, other org sees nothing); the worker made all three `ready` (FAQ = exactly 1 chunk starting `Q: `, all vectors 768-dim, right org); reprocess of a ready document queued it, a second reprocess was 409 `already_processing`, and chunk count stayed the same; deleting a document whose Storage object was already gone succeeded (real Supabase answers that DELETE with HTTP 400 and `statusCode` "404" / `not_found`); cross-org delete and reprocess were 404 and changed nothing; delete removed row, chunks, object and a pending queued job. Afterwards the script confirmed 0 orgs, documents, chunks, jobs and Storage objects. The first run had one false failure from a script bug (it counted completed jobs, which delete keeps by design). The HTTP routes, JWT auth and RBAC were not exercised live (unit tests and the endpoint matrix cover them).

## What the API code looks like

```
apps/api/app/
  config.py         Settings from env (DATABASE_URL, SUPABASE_URL, CORS_ORIGINS, LLM and Gemini settings, ...). `missing_required()` makes boot fail if DATABASE_URL or SUPABASE_URL is empty, if LLM_PROVIDER is unset or not fake/gemini, or (gemini) if GEMINI_API_KEY, EMBED_BATCH_SIZE, CHUNK_MAX_CHARS, GEMINI_BASE_URL or the model names are bad
  auth.py           verify_token -> Identity(user_id, email); get_identity; get_current_user (profiles row); require_role(...)
  errors.py         ApiError + handlers: every error is {"error": {"code", "message"}}, incl. 404, 422 and unexpected 500
  db/pool.py        psycopg pool; `Conn` = the connection dependency to use in routes (see decisions)
  db/pool.py        also `borrow_conn()`: a pooled connection for non-request code (the worker)
  db/queries/       profiles.py, orgs.py, documents.py (list/count/get_for_update/delete, mark_processing/queued/failed/ready), chunks.py (replace_chunks) - raw SQL
  db/jobs.py        enqueue, delete_queued_ingest_jobs, claim_next (SKIP LOCKED), lock_running, complete, fail (all fenced by attempts), requeue_stale (returns a StaleJob list). claim_next and requeue_stale are global on purpose
  storage.py        Storage protocol (upload, delete, download) + SupabaseStorage (httpx, service-role key); StorageObjectNotFound; delete and download both treat 404 and the 400 not-found body as missing; tests use FakeStorage
  services/         onboarding.py, documents.py (upload, create_faq, list, reprocess, delete), parsing.py (pypdf/md/txt/faq -> pages; format_faq; PermanentIngestError), chunking.py, ingestion.py (ingest_job: begin / slow work / finalize; classify() treats any LLMError as a provider failure; a provider Retry-After lengthens the requeue delay, never shortens it, capped at 300 s)
  llm/              base.py (LLMError family: EmbeddingError, LLMRateLimited, LLMUnavailable, LLMBlocked, LLMMalformedOutput, LLMAuthError, LLMBadRequest; EmbedKind; Embedder protocol; LLMProvider adds generate; embed_in_batches(kind=))
                    gemini.py (GeminiProvider over httpx: batchEmbedContents 768-dim, normalised, task types; generateContent with JSON mime type and schema; status -> exception table; no retries inside), structured.py (generate_validated: Pydantic check, one retry on LLMMalformedOutput only)
                    fake.py (deterministic 768-dim; records kinds and generate_calls; queue_generate scripts generate, an empty queue asserts), prompts.py (data_block with random nonce, DATA_NOTICE; the real prompt templates come with steps 10 and 14)
                    __init__.py get_provider(): fake (uncached, one-time warning) or cached GeminiProvider; clear_provider_cache()
  worker.py         process_one, sweep_stale, IngestionWorker (daemon thread; depends on the narrow Embedder protocol), build_worker; started and stopped in the main.py lifespan
  routes/           me.py, onboarding.py, org.py, documents.py
  main.py           lifespan starts the worker after open_pool (worker disabled with a logged error if build_worker fails) and stops it before close_pool; also holds a middleware that refuses oversized or length-less POST /documents bodies (10 MB) and POST /documents/faq bodies (36 KB) before parsing
apps/api/tests/     conftest.py has FakeDb / FakeConn, FakeDocs (in-memory documents and jobs, `install(monkeypatch)`), make_token() and the `two_orgs` fixture (orgs A and B, each with admin, agent, customer)
                    endpoint_matrix.py: one row per /api/v1 route; test_tenancy.py: completeness + 401/403/org_id-from-client tests
                    test_me, test_onboarding, test_documents_upload, test_documents_manage, test_storage, test_auth_hardening, test_db_scope, test_health,
                    test_ingestion_service, test_worker, test_parsing, test_chunking, test_llm_fake, test_llm_gemini (mocked httpx transport), test_llm_provider, test_llm_structured, test_llm_prompts (no network; conftest has an autouse fixture setting WORKER_ENABLED=false)
  integration/      real-Postgres tests (skipped unless TEST_DATABASE_URL is set): supabase_stubs.sql, conftest.py, test_onboarding_sql.py, test_documents_sql.py, test_jobs_sql.py, test_chunks_sql.py
```

## Environment

- `apps/api/.env` exists locally and is filled in (gitignored). `SUPABASE_JWT_SECRET` is **not used**: this project signs tokens with ES256 and the API verifies them against the public JWKS. **`LLM_PROVIDER` is now required, with no default**: `fake` (local dev, CI) or `gemini`; unset, unknown or (for gemini) a missing `GEMINI_API_KEY` or an invalid LLM-related setting makes the API refuse to boot (the lifespan checks `missing_required()`). The local `.env` already sets it. New Gemini settings: `GEMINI_EMBED_MODEL` (default `gemini-embedding-001`), `GEMINI_GENERATION_MODEL` (default `gemini-3.5-flash-lite`; the user still has to check the AI Studio free quota for it), `GEMINI_BASE_URL` (https only), `LLM_TIMEOUT_SECONDS` (30). `GEMINI_API_KEY` exists only on the API, never in the web app. **Deploy note:** Render must have `LLM_PROVIDER` and `GEMINI_API_KEY` set before this branch deploys, otherwise the health check fails and Render keeps the previous instance. Other optional settings: `WORKER_ENABLED`, `WORKER_POLL_SECONDS`, `WORKER_STALE_SECONDS`, `WORKER_SWEEP_SECONDS`, `INGEST_RETRY_BASE_SECONDS`, `EMBED_BATCH_SIZE`, `CHUNK_MAX_CHARS`, `CHUNK_OVERLAP_CHARS`, `PARSE_DEADLINE_SECONDS`.
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
- **Ingestion (step 7), chosen by the user:** fake embeddings at the time (the Gemini provider followed in step 9); chunks of 2800 characters (about 700 tokens) with 400 overlap; 3 attempts with 30 s then 120 s backoff; poll every 2 s; worker is a daemon thread in the API process; no migration; stale jobs requeued after 600 s. Full flow in `docs/design.md` (Ingestion). Fencing token is `jobs.attempts`; every document or chunk write follows `lock_running` (`FOR UPDATE`) in the same transaction. `documents.error` holds only fixed readable sentences; technical text goes to `jobs.last_error` (500 chars) and the log. No DB connection is held during download, parse or embed.
- **Documents management (step 8), approved by the user:** FAQ text is a Storage object `faq.txt` (no migration; see design.md). Delete is Storage first, then row, queued jobs and chunks in one transaction; Storage failure returns 502 and keeps the row; allowed in any status. Reprocess only from `ready` or `failed` (409 otherwise, or if there is no file). List is `limit`/`offset` (default 50, max 100), newest first, `{items, total}`. FAQ limits 300 and 2400 characters, no edit (delete and re-add), no `/source` endpoint or web page yet. Code review made the FAQ endpoint require `Content-Length` (411) and set its cap to 36 KB so JSON-escaped emoji still fit.
- **Gemini provider (step 9), approved by the user:** `gemini-embedding-001` with task types (`RETRIEVAL_DOCUMENT` for chunks, `RETRIEVAL_QUERY` for questions), 768 dimensions, L2-normalised; `gemini-3.5-flash-lite` as the default generation model. A bad LLM config crashes the API at boot (no silent fallback). `LLM_PROVIDER` is explicit with no default, plus the key check (no `APP_ENV`). A provider `Retry-After` is honoured, capped at 300 s. Scope additions: `kind` on `embed` and the `data_block` helper. The key goes only in the `x-goog-api-key` header; error messages are fixed text with no URL, body or key. The provider never retries; `generate_validated` retries once on malformed output; transient failures are the job queue's business. Full detail in `docs/design.md` section 5 (LLM provider).
- **Jobs:** `claim_next` is one UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1). It and `requeue_stale` are the only queries not scoped by `org_id`; the worker must use the claimed job's `org_id` afterwards. CLAUDE.md rule 1 now states this exception.
- UI-first on the Ask page was a deliberate deviation from plan order (step 12 before steps 4 to 11).
- The user is a front-end engineer learning backend: explain decisions in plain language, keep summaries short.

## Known gaps and risks

- Unit tests use in-memory fakes, which hide real SQL, transactions and constraints. Step 5 added real-Postgres tests for onboarding, but they run only in CI (see above) and cover only `create_org`, `join_org`, `get_profile` and the join-code table. Each later step that adds SQL should add integration tests too.
- The integration conftest DROPS the `public` and `auth` schemas. It refuses non-local hosts and any URL with a query string. Never point `TEST_DATABASE_URL` at Supabase or an SSH tunnel to it.
- Atomicity tests must leave the connection idle (`conn.commit()`) before calling the service and must not `conn.rollback()` afterwards, otherwise `conn.transaction()` is only a savepoint and the test cannot fail.
- The route-completeness test uses a FastAPI internal (`effective_route_contexts`) because `include_router` routes are lazy in this FastAPI version. If a FastAPI upgrade breaks it, fix `_registered_routes()` in `tests/test_tenancy.py`.
- Upload holds a pooled DB connection (max 5) while it parses the PDF and calls Storage (up to 30 s), and **delete holds a connection plus the document's row lock across its Storage call**. Five slow concurrent uploads or deletes can make other requests return 503 `db_unavailable`, and a worker `mark_ready` for that document waits on the lock. Fix later by releasing the connection before the Storage call (for delete: lock and check, commit, call Storage, then delete the row; or delete the row first and remove the file best effort, which risks an orphan file). Delete also has a window where the row outlives its file if the DB step fails after Storage succeeded (retry fixes it). (The worker does not have this problem: it holds at most one connection, only for short steps.)
- **Hostile PDFs:** the 60 s parse deadline is checked only between pages. One pathological page can make pypdf run long (pure Python, holds the GIL, so it can stall the whole API process), and the 2M character cap applies after `extract_text()` returns, so memory is not bounded either. A subprocess with a hard timeout is the real fix.
- The worker claims every job type. Any `triage_ticket` job would be failed as "unknown job type"; step 14 must add a handler dispatch (or a type filter on `claim_next`) before it enqueues triage jobs.
- FAQ chunk text starts with `Q: ` and `A: ` prefixes and its `section` is the question (cut to 200 characters); step 10 should know that when building citation snippets. `GET /documents/{id}/source` for a FAQ must read the Storage object.
- A document stuck in `processing` (worker crash) returns 409 on reprocess until the 600 s stale sweep.
- Test doubles diverge from Postgres: `FakeDocs.get_document_for_update` is a plain get (no lock) and its list slices in Python, so lock behaviour and SQL ordering are covered only by CI integration tests.
- Storage errors are transient except a 404, or a 400 whose body says not found (Supabase answers a missing object that way; seen live on download in the step 7 check and on delete in the step 8 check). A permanently oversized object is retried up to 3 times, then fails with the generic message.
- If `_begin` or `_record_failure` itself hits a database error, the job stays `running` until the stale sweep (600 s) handles it.
- **Gemini is unverified.** Every REST field name (`outputDimensionality`, `taskType`, `responseSchema`, the `embeddings[].values` and `candidates` shapes), the model ids and the free-tier limits were written from memory and never checked against the live API. A live check (needs the user's go-ahead and the key on the API only) must come before real vectors are trusted. The field names are constants at the top of `gemini.py`.
- Gemini may not send `Retry-After` on a 429, and the 429 body is deliberately not parsed (it could carry request text), so the retry delay may fall back to the 30 s / 120 s backoff.
- A `MAX_TOKENS` finish is treated as malformed output, and `generate_validated` then retries the identical request, which will likely truncate again.
- A job retry re-embeds every batch of the document, so a failure on a late batch repeats the early batches' quota use.
- A permanent provider error (`LLMAuthError`, `LLMBadRequest`, `LLMBlocked`, or a bad embedding response) fails the document at once with the "try again later" sentence; after fixing the key the admin must reprocess each failed document.
- The shared `httpx` client inside the cached `GeminiProvider` is never closed at shutdown.
- An invalid `CHUNK_OVERLAP_CHARS >= CHUNK_MAX_CHARS` raises inside the job and burns its retries; there is no startup validation.
- `ruff format --check` flags a few older files (`orgs.py`, `tests/integration/conftest.py`, `test_onboarding.py`); CI does not enforce formatting.
- A strict `%PDF-` check at byte 0 rejects the rare valid PDF with junk before the header (415).
- The new integration tests clean the `jobs` and `documents` tables in a fixture that must run before `World.cleanup()` (those tables reference orgs without cascade).
- No rate limiting. Join-code guessing (2^40 codes) and unlimited org creation are deferred to step 16.
- Join codes cannot be rotated or expire. Orgs created by the future seed script need a code inserted (`GET /org/join-code` returns 404 for an org without one).
- CI has a Postgres service container (added in step 5) and was green on `529e8fe`.
- The Starlette test-client `httpx` deprecation warning is known and harmless.

## Step 10 starting notes

- Embed the question with `embed(..., kind="query")` (chunks were embedded as `document`). Call `generate_validated` with a hand-written small schema dict (for example `{answer, cited_chunk_ids}`), not `model_json_schema()`.
- Wrap retrieved chunks and the question in `data_block(...)` and include `DATA_NOTICE` in the system prompt (rule 9). The real prompt templates go in `llm/prompts.py`.
- Build the 502 `llm_unavailable` handler: map any `LLMError` to a fixed retryable message with no provider text (AC-21). It does not exist yet, so an `LLMError` in a route would currently be a 500.
- The similarity threshold has to be tuned on real vectors in step 11, which needs the live Gemini check first. Fake vectors say nothing about it.
- FAQ chunk text starts with `Q: ` and `A: ` (see known gaps) when building citation snippets.
- Documents UI is still not built: the web Documents page (list with polling while any row is `queued` or `processing`, upload, FAQ form, delete, reprocess) belongs with the UI steps. All the API it needs exists.
- Deferred from step 5: a rerunnable live end-to-end check script in `apps/api/scripts/` (needs the user's approval each run).
- Branch cleanup (ask before deleting anything): `git branch` shows merged `feat/*` and `docs/*` branches and many `worktree-agent-*` branches; `.claude/worktrees/` holds the agent worktrees (untracked). Remove with `git worktree remove` once nobody needs them.

## Useful checks

- `npx supabase migration list --linked` shows local vs remote migrations.
- `npx supabase db query --linked --read-only "<sql>"` runs read-only SQL on the linked database.
- API from `apps/api`: `.venv\Scripts\ruff check .`, `.venv\Scripts\mypy`, `.venv\Scripts\pytest`.
