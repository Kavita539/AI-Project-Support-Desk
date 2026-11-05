# AI Project Support Desk

Multi-tenant support desk: customers ask questions answered from an org's uploaded docs (RAG with citations); unresolved questions become tickets; AI drafts replies that a human approves.

## Stack

- Web: React + Vite + TypeScript, React Router, TanStack Query, supabase-js (anon key only)
- API: Python 3.12, FastAPI, Pydantic v2, psycopg 3 (raw SQL, no ORM)
- Data: Supabase Postgres + pgvector (`vector(768)`), Supabase Auth, private Storage bucket
- LLM: Gemini free tier behind an `LLMProvider` interface; fake provider in tests/CI
- Jobs: Postgres `jobs` table, worker loop inside the API process
- Hosting: Vercel (web), Render (API), Supabase

## Commands

Run from the folder shown. Shell is Windows; the venv lives in `apps/api/.venv`.

API (`apps/api`):
- Setup: `py -3.12 -m venv .venv`, then `.venv\Scripts\pip install -e ".[dev]"`
- Run: `.venv\Scripts\uvicorn app.main:app --reload`
- Test: `.venv\Scripts\pytest`
- Lint: `.venv\Scripts\ruff check .`
- Typecheck: `.venv\Scripts\mypy`

Web (`apps/web`):
- Setup: `npm install`, then `npx playwright install chromium` (once)
- Run: `npm run dev` (http://localhost:5173)
- Test: `npm test` (vitest); e2e: `npm run e2e` (Playwright)
- Lint: `npm run lint`; typecheck: `npm run typecheck`; build: `npm run build`

CI (`.github/workflows/ci.yml`) runs all of the above. Run lint, typecheck and tests before calling work done.

## Folder structure

```
apps/web/src/   lib/ (supabase + api client)  auth/  components/
                pages/{documents,ask,tickets,users}/
apps/api/app/   main.py  config.py  auth.py  worker.py
                routes/    thin: auth + input checks, then call a service
                services/  business rules (ingestion, retrieval, answering, triage, approval)
                llm/       base.py (interface), gemini.py, fake.py, prompts.py
                db/        pool.py, queries/ (SQL by area), jobs.py
                scripts/   seed_demo.py, run_eval.py
apps/api/tests/ unit, API, tenancy, injection, eval fixtures
supabase/migrations/   schema, pgvector, indexes, RLS
docs/                  prd.md, design.md, plan.md
```

## Hard rules (from docs/design.md)

1. Tenant isolation: `org_id` and `role` come only from the verified JWT plus the `profiles` row. Never accept `org_id` from request bodies or query strings. Every query in `db/` takes `org_id` explicitly. RLS is a backstop, not the primary control.
2. Cross-org IDs return 404, not 403. Wrong role returns 403. Missing/invalid token returns 401.
3. Fixed pipeline, no agent: classify, retrieve, draft. The LLM has no tools and no write access; its output is schema-validated JSON that the server stores.
4. Citations are server-verified: drop any cited chunk ID not in the retrieved set. If no valid citations remain, return the "I don't know" response.
5. The "I don't know" gate runs before the LLM: top similarity below threshold means skip generation.
6. Drafts are append-only and gated: `ai_text` is written once and never updated. `final_text`, `approved_by`, `approved_at` are set only by the approve endpoint. Customer-facing serializers read only approved drafts and have no `ai_text` field.
7. Ticket status changes go through the single `transition()` function; invalid transitions return 409.
8. Jobs are idempotent: claim with `FOR UPDATE SKIP LOCKED`; re-ingest deletes and inserts chunks in one transaction; one pending draft per ticket.
9. Document and ticket text is untrusted: put it in delimited data blocks, tell the model it is data, keep the injection test fixture passing.
10. Secrets: the web holds only the Supabase anon key. The service-role key and LLM key exist only on the API.
11. CI and tests never call the real LLM; use the fake provider.
12. Raw SQL, no ORM. Errors use `{"error": {"code", "message"}}`. API base path is `/api/v1`.

## Workflow for every feature

1. `git fetch`, check the previous branch is merged into `origin/main`, fast-forward local `main`, then branch `feat/<name>` from it. Never add commits to an already-merged branch.
2. Run lint, typecheck and tests before committing. Commit only; push and apply migrations only when the user asks.

## Docs are part of done

Docs are updated in the same branch as the code, never later. Before calling a step done:
- `docs/plan.md`: tick the step; add or reword steps if scope changed.
- `docs/handoff.md`: rewrite "Where we are", the code map, decisions, known gaps and next steps so a fresh session starts from the truth. Verify facts (test count, migrations, stash, environment) instead of copying old text.
- `docs/design.md` and `docs/prd.md`: update any endpoint, table, status code or flow that changed.
- Remove statements that are no longer true; do not leave stale text behind.

## More detail

- `docs/handoff.md`: current state, decisions and next steps. Read first in a new session
- `docs/prd.md`: product requirements and acceptance criteria (AC-n)
- `docs/design.md`: architecture, data model, API, pipelines, milestones
- `docs/plan.md`: step-by-step build plan and progress
