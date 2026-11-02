# Session handoff

Read this first in a new session, then `CLAUDE.md` and `docs/plan.md`. No secrets belong in this file.

## Where we are

- Plan steps 1 to 3 are done. Step 4 (JWT verification, `profiles` lookup, `GET /api/v1/me`) is next.
- No FastAPI code exists yet. `apps/api` is only the scaffold.
- Supabase project `support-desk` is created, linked and migrated. Project ref: `yiakeyeohwpbjjseugha` (region ap-south-1, free plan).
- Both migrations are applied to it: `20261028000001_schema.sql` (9 tables, pgvector 0.8.2) and `20261028000002_rls.sql` (RLS on all 9 tables, select-only policies, `jobs` has none).
- The older paused project `task-manager` in the same Supabase org is unrelated. Do not use it.

## Stashed work: Ask page UI

The customer Ask page was built first, against a mock API, then stashed so the branch stays backend-focused.

- Restore: `git stash pop` (stash message "Ask page UI with mock API"), then `npm install` in `apps/web`.
- Contents: `AskPage`, `TurnView`, `CitationList`, `Loading`, `DemoBanner`, `lib/api.ts` (mock, swap for real `/api/v1` calls later), `lib/types.ts`, `index.css`, two page tests, and the `@testing-library/user-event` dev dependency.
- It passed lint, typecheck, vitest, build and the Playwright smoke test when stashed.
- Not built yet: login, role routing, documents page, tickets, review UI.

## Next: step 4 needs `apps/api/.env`

Copy `.env.example` to `apps/api/.env` (gitignored) and fill in from the Supabase dashboard for the project above:

| Variable | Where |
|---|---|
| `SUPABASE_URL` | Project Settings > API (Project URL) |
| `SUPABASE_SERVICE_ROLE_KEY` | Project Settings > API Keys. API only, never the web |
| `SUPABASE_JWT_SECRET` | Project Settings > API > JWT settings (check whether the project uses the newer asymmetric signing keys before choosing how to verify tokens) |
| `DATABASE_URL` | Connect button > connection string, using the pooler. Needs the DB password |
| `GEMINI_API_KEY` | Not needed until step 9. Use `LLM_PROVIDER=fake` until then |

The web `.env.local` takes only `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.

## Decisions made this session

- Migrations are applied with the Supabase CLI: `npx supabase db push --linked` (preview with `--dry-run`). Do not paste SQL in the dashboard, so migration history stays in sync.
- UI-first on the Ask page was a deliberate deviation from plan order (step 12 before steps 4 to 11).
- The user is learning the backend and wants decisions explained in plain language. Keep summaries short.

## Useful checks

- `npx supabase migration list --linked` shows local vs remote migrations.
- `npx supabase db query --linked "<sql>"` runs SQL on the linked database.
