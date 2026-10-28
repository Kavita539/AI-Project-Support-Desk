# AI Project Support Desk: Design (MVP)

Status: Draft. Implements `docs/prd.md`. Covers frontend and backend.
Stack and tradeoffs were decided in the earlier architecture proposal; this doc builds on them and does not reopen them.

## 1. Stack

```
React (Vite, TS) --JWT--> FastAPI --> Supabase Postgres (+pgvector), Storage
        |                    |
   Supabase Auth             +--> Gemini API (embeddings + generation)
```

| Layer | Choice | Host |
|---|---|---|
| Web | React + Vite + TypeScript, React Router, TanStack Query | Vercel |
| API | Python 3.12, FastAPI, Pydantic v2, psycopg 3 (raw SQL) | Render free web service |
| DB / vectors | Supabase Postgres + pgvector | Supabase free |
| Auth | Supabase Auth (email/password, Google optional) | Supabase free |
| Files | Supabase Storage (private bucket) | Supabase free |
| LLM / embeddings | Gemini free tier behind `LLMProvider` | Free, rate-limited |
| Jobs | Postgres `jobs` table + worker loop in the API process | none |
| CI | GitHub Actions: ruff, pytest, web build | free |

Supporting choices:

- **Raw SQL over an ORM.** Queries are few and the vector search and `FOR UPDATE SKIP LOCKED` are SQL-native. Every query takes `org_id` explicitly, which keeps AC-5 easy to audit.
- **TanStack Query** gives polling for document status (AC-10) and loading states (AC-22) with little code.
- **PDF parsing:** `pypdf` (text only). A PDF with no extractable text fails with a reason (AC-9).
- **Embedding dimension:** 768, set through Gemini's output-dimensionality option. Stored as `vector(768)`. Confirm in M3 (PRD open item).

## 2. Key design decisions

1. **Tenant isolation, two layers.** The API connects with the service role, derives `org_id` and `role` from the verified JWT plus the `profiles` row, and passes `org_id` into every query. RLS policies are enabled on all tenant tables as a backstop (policies key on `profiles` for the JWT's `sub`). Request bodies and query strings never supply `org_id`. Cross-org IDs return 404, not 403.
2. **Fixed pipeline, no agent.** Classify, retrieve, draft. The LLM has no tools and no write access. Its output is JSON that the server validates and then stores.
3. **Citations are server-verified.** The model is given retrieved chunks with their IDs and returns IDs. The server drops any ID not in the retrieved set (AC-16). If an answer ends up with no valid citations, return the "I don't know" response.
4. **"I don't know" gate runs before the LLM.** If the top chunk's similarity is below the threshold, skip generation and return the fallback. The threshold is tuned on the eval set (M3).
5. **Drafts are append-only and gated.** `ai_text` is written once and never updated. `final_text`, `approved_by` and `approved_at` are set only by the approve endpoint. Customer-facing serializers read from `drafts` only where `approved_at IS NOT NULL`, and have no field for `ai_text` (AC-33, AC-37).
6. **Jobs table, idempotent.** The worker claims jobs with `FOR UPDATE SKIP LOCKED`. Ingestion deletes the document's chunks, then inserts new ones in one transaction (AC-11). The pipeline job upserts a single active draft per ticket (AC-32). Stale `running` jobs are re-queued after a timeout, which covers host sleep or restart.
7. **Provider interface.** `embed(texts) -> list[vector]` and `generate(prompt, schema) -> dict`. A fake implementation is used in CI (AC-42).
8. **Auth on the web.** `supabase-js` handles sign-up, login and session. The web sends `Authorization: Bearer <access_token>` to the API. The web holds only the anon key. The service key and the LLM key exist only on the API (AC-39).

## 3. Data model

All tenant tables carry `org_id uuid not null references orgs(id)` and an index on it. IDs are `uuid` (`gen_random_uuid()`), timestamps are `timestamptz`.

```
orgs(id, name, created_at)

profiles(user_id PK -> auth.users, org_id, role, email, created_at)
  role: 'admin' | 'agent' | 'customer'

documents(id, org_id, name, kind, storage_path, status, error, page_count,
          size_bytes, created_by, created_at)
  kind:   'pdf' | 'md' | 'txt' | 'faq'
  status: 'queued' | 'processing' | 'ready' | 'failed'

chunks(id, org_id, document_id -> documents ON DELETE CASCADE, content,
       embedding vector(768), page int null, position int, section text null,
       created_at)
  unique (document_id, position)           -- idempotent re-ingest
  index: hnsw (embedding vector_cosine_ops); btree (org_id, document_id)

questions(id, org_id, user_id, query, answer text null, status,
          retrieved_chunk_ids uuid[], citations jsonb, top_score real,
          helpful bool null, created_at)
  status: 'answered' | 'no_answer' | 'error'

tickets(id, org_id, customer_id, question_id null, subject, body, status,
        category null, priority null, created_at, updated_at)
  status:   'open' | 'awaiting_review' | 'needs_manual_reply' | 'answered' | 'closed'
  category: 'Billing' | 'Technical' | 'Account' | 'Other'
  priority: 'Low' | 'Medium' | 'High'
  context: ai_answer text, retrieved_chunk_ids uuid[]   -- snapshot at creation (AC-24)

ticket_messages(id, org_id, ticket_id, author_id, author_role, body, created_at)
  -- customer notes and follow-ups; never holds unapproved AI text

drafts(id, org_id, ticket_id, ai_text null, citations jsonb, source text,
       working_text null, final_text null, approved_by null, approved_at null,
       state, created_at)
  source: 'ai' | 'manual'
  state:  'pending' | 'approved' | 'rejected'
  unique (ticket_id) where state = 'pending'   -- one active draft (AC-32)
  check (state <> 'approved' or (final_text is not null and approved_by is not null and approved_at is not null))

jobs(id, org_id, type, payload jsonb, status, attempts, max_attempts, run_after,
     locked_at null, last_error null, created_at)
  type:   'ingest_document' | 'triage_ticket'
  status: 'queued' | 'running' | 'done' | 'failed'
```

Notes:

- A rejected draft stays as a row with `state='rejected'`, so history is kept. Regenerate and manual reply each create a new `pending` draft.
- A manual reply is a draft with `source='manual'` and `ai_text` null. It goes through the same approve step (AC-36).
- Storage path: `{org_id}/{document_id}/{filename}`, in a private bucket. Source links are short-lived signed URLs created by the API.
- Allowed ticket transitions (AC-26):
  `open -> awaiting_review | needs_manual_reply`, `awaiting_review -> answered | needs_manual_reply`, `needs_manual_reply -> awaiting_review | answered`, `answered -> closed`, `open -> closed`. Enforced in one `transition()` function.
- Retrieval SQL shape (the `org_id` argument always comes from the auth dependency):
  `SELECT id, document_id, content, page, 1 - (embedding <=> $q) AS score FROM chunks WHERE org_id = $org ORDER BY embedding <=> $q LIMIT $k`.

## 4. API

Base path `/api/v1`. JSON throughout. All endpoints except `/health` need a valid bearer token (401 otherwise). Role checks are dependencies: `require_role('admin')`, `require_role('admin','agent')`, or any authenticated user.

Errors use `{ "error": { "code": "...", "message": "..." } }`. The status codes used are 400, 401, 403, 404, 409 (invalid transition), 413 and 415 (upload limits and types), 429, and 502 (LLM provider failure, retryable).

### Auth / session
| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/health` | public | Liveness; also used to warm the host |
| GET | `/me` | any | `{user_id, email, org_id, role}`; creates the profile and org on first login (first user of a new org is Admin) |
| GET | `/org/users` | admin | List org users |
| PATCH | `/org/users/{id}` | admin | Set a user's role (basic assignment flow) |

### Documents
| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/documents` | admin | List with status and error |
| POST | `/documents` | admin | Multipart upload (`.pdf/.md/.txt`, 10 MB, 100 pages); stores the file, inserts a `queued` row and an `ingest_document` job |
| POST | `/documents/faq` | admin | `{question, answer}` creates a `faq` document and job |
| GET | `/documents/{id}/source` | any | Signed URL (or the FAQ text) for citation links; same-org only |
| POST | `/documents/{id}/reprocess` | admin | Re-enqueue ingestion (idempotent) |
| DELETE | `/documents/{id}` | admin | Remove file, chunks and embeddings |

The page-count limit (AC-7) is checked in the upload handler for PDFs, before the job is queued.

### Ask
| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/ask` | customer, agent, admin | `{query}` returns `{question_id, status, answer, citations[]}`; rate-limited (AC-23). Runs retrieve, gate, generate, citation check |
| POST | `/questions/{id}/feedback` | owner | `{helpful: bool}` (AC-20) |
| GET | `/questions` | owner | Own history (optional, small) |

Citation shape: `{chunk_id, document_id, document_name, page, section, snippet}`.

### Tickets
| Method | Path | Role | Purpose |
|---|---|---|---|
| POST | `/tickets` | customer | `{question_id, notes}`; copies the question, answer and chunk IDs; enqueues `triage_ticket` (AC-24). Only allowed when the question was marked not helpful or was `no_answer` |
| GET | `/tickets` | any | Customer: own. Agent/admin: all in org. Filters `status`, `category` (agent/admin only) |
| GET | `/tickets/{id}` | any | Customer view: ticket, messages, approved reply only. Staff view: also category, priority, context and sources, and the pending draft |
| POST | `/tickets/{id}/messages` | any (owner or staff) | Add a note or follow-up |
| PATCH | `/tickets/{id}` | agent, admin | Change category/priority, close (transition-checked) |

### Drafts / approval
| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/tickets/{id}/draft` | agent, admin | Current pending draft with citations |
| PUT | `/drafts/{id}` | agent, admin | Edit the working text (pending drafts only; does not touch `ai_text`) |
| POST | `/drafts/{id}/approve` | agent, admin | Writes `final_text`, `approved_by`, `approved_at`; ticket becomes `answered` |
| POST | `/drafts/{id}/reject` | agent, admin | Marks rejected |
| POST | `/tickets/{id}/draft/regenerate` | agent, admin | Enqueue a new AI draft |
| POST | `/tickets/{id}/draft/manual` | agent, admin | Create a manual pending draft `{text}` |

An edit before approval is held in the draft row as the working text and becomes `final_text` on approval. `ai_text` is never overwritten (AC-35). Implementation detail: a `working_text` column on `drafts` holds the in-progress edit.

### Dev / demo
`python -m app.scripts.seed_demo` (CLI, not an endpoint) seeds the demo org, sample docs, the eval set and users (AC-43). `python -m app.scripts.run_eval` runs the answer and "I don't know" eval (AC-15, AC-18).

## 5. Pipelines

**Ingestion (`ingest_document`)**: claim job, set `processing`, download the file, extract text (per page for PDF), chunk to about 500 to 800 tokens with overlap and keep page/section, embed in batches, then in one transaction delete old chunks and insert new ones and set `ready`. Any failure sets `failed` with a readable reason (empty text, corrupt file, provider error after retries). Other jobs are unaffected (AC-9).

**Ask (synchronous)**: embed query, top-k by cosine on `org_id`, apply the threshold, build the prompt (system rules, chunks as delimited untrusted data, question), call `generate` with a JSON schema `{answer, cited_chunk_ids}`, validate, drop invalid citations, store the question row. Provider errors return 502 with a retryable message and store nothing as final (AC-21).

**Triage (`triage_ticket`)**: step 1 classify to `{category, priority}`, with one retry on schema failure and fallback `Other/Medium` (AC-29). Step 2 retrieve using the ticket text. Step 3 draft with citations. Insert the draft and set `awaiting_review`. After max attempts, set `needs_manual_reply` (AC-31). The job as a whole is idempotent (AC-32).

**Prompt-injection posture**: document and ticket text is placed inside delimited data blocks, the model is told it is data, output is schema-validated, and there are no tools. A test document with injected instructions is part of the test suite (AC-38).

## 6. Repository structure

```
ai-support-desk/
|- apps/
|  |- web/                       # React + Vite + TS
|  |  |- src/
|  |  |  |- main.tsx, App.tsx    # router, providers
|  |  |  |- lib/                 # supabase client, api client (adds bearer), types
|  |  |  |- auth/                # AuthProvider, RequireRole, login/signup
|  |  |  |- pages/
|  |  |  |  |- documents/        # Admin: list + status polling, upload, FAQ form
|  |  |  |  |- ask/              # Customer: chat, citations, feedback, create ticket
|  |  |  |  |- tickets/          # list (+filters), detail, agent review panel
|  |  |  |  |- users/            # Admin: org users
|  |  |  |- components/          # CitationList, StatusBadge, Loading, DemoBanner
|  |  |- package.json, vite.config.ts
|  |- api/                       # FastAPI
|     |- app/
|     |  |- main.py              # app factory, router mount, worker startup
|     |  |- config.py            # env settings
|     |  |- auth.py              # JWT verify, current_user, require_role
|     |  |- routes/              # documents.py ask.py tickets.py drafts.py org.py
|     |  |- services/            # ingestion.py retrieval.py answering.py triage.py approval.py
|     |  |- llm/                 # base.py (interface), gemini.py, fake.py, prompts.py
|     |  |- db/                  # pool.py, queries/ (SQL by area), jobs.py
|     |  |- worker.py            # claim/run/retry loop
|     |  |- scripts/             # seed_demo.py, run_eval.py
|     |- tests/                  # unit, API, tenancy, injection, eval fixtures
|     |- pyproject.toml
|- supabase/migrations/          # schema, pgvector, indexes, RLS
|- .github/workflows/ci.yml
|- docs/                         # prd.md, design.md
|- README.md, .env.example
```

Routes stay thin: they check auth and input, then call a service. Services own business rules (transitions, citation checks, approval). Queries live in `db/` and always take `org_id`.

## 7. Frontend

- **Routing by role** (F1): Admin lands on `/documents`, Agent on `/tickets`, Customer on `/ask`. `RequireRole` hides routes the role cannot use. This is UX only; the API enforces access.
- **Ask page:** message list, "thinking" state after 3 s with a cold-start hint (AC-22), citations with document name, page/section, snippet and source link, Helpful / Not helpful buttons, and "Create ticket" on Not helpful or "I don't know" with notes (F4).
- **Documents page:** table polling every few seconds while any row is `queued` or `processing`. Client-side type/size checks mirror the server limits and the server errors are shown verbatim.
- **Tickets:** customers see their own list and an approved reply; staff get filters and a review panel (context, category, priority, editable draft, sources, Approve / Reject / Regenerate / Write manually).
- **Global:** `DemoBanner` (AC-40), error toasts for 429 and 502 with retry, API warm-up ping on app load.

## 8. Testing and CI

- **Backend:** pytest with the fake LLM. Required suites: tenancy (two seeded orgs, every endpoint, guessed IDs return 404), role matrix (403), ingestion idempotency and failure paths, citation filtering, ticket transitions, approval invariants (no customer path exposes unapproved `ai_text`), injection fixture, rate limit.
- **Eval (manual, real Gemini):** `run_eval` over the 20 to 30 in-scope and the out-of-scope questions, reporting accuracy and "I don't know" rates against AC-15 and AC-18.
- **CI:** ruff, pytest, `npm run build` (with type check) on every push. No network LLM calls.

## 9. Milestone mapping (suggested order)

1. M1 Schema, migrations, auth, `/me`, tenancy tests.
2. M2 Documents upload, jobs table, ingestion worker.
3. M3 Retrieval, `/ask`, citations, threshold tuning, eval set.
4. M4 Ask UI and feedback.
5. M5 Tickets and triage pipeline.
6. M6 Review and approval UI, rate limits, hardening.
7. M7 Deploy, README, seed script, CI polish.

## 10. Remaining open items

- Exact Gemini model names and the embedding dimension (768 assumed). Confirm against current provider docs before the M1 migration.
- Similarity threshold, top-k, chunk size and rate limit values. Tune in M3 and M6.
- Whether the worker runs inside the API process (default here) or as a second Render service. A second service would avoid idle sleep, but it is not free-tier-safe.
- Weekly time budget and target dates for the milestones above.
