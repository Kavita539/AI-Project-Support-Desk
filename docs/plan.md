# Build Plan

Milestones follow `docs/design.md` section 9. Tick a step only when lint, typecheck and tests pass.

## M0 Scaffold
- [x] 1. Repo scaffold: `apps/web`, `apps/api`, `supabase/`, CI workflow, `.env.example`, CLAUDE.md

## M1 Schema, auth, tenancy
- [x] 2. Migrations: orgs, profiles, documents, chunks (`vector(768)`), tickets, drafts, jobs, indexes
- [x] 3. RLS policies as backstop (applied to the linked Supabase project)
- [x] 4. JWT verification, `profiles` lookup, `GET /api/v1/me`
- [x] 4b. Onboarding: `POST /onboarding` creates an org (caller is Admin) or joins one by join code (caller is Customer); `GET /org/join-code`; `require_role`. `/me` stays read-only
- [x] 5. Tenancy tests: two-org fixture, endpoint matrix with a completeness test (a new route without a row fails), 401/403 and `org_id`-from-client checks, real-Postgres integration tests in CI. The cross-org 404 test is wired but runs zero cases until the first ID-based route (step 6) adds a matrix row with `cross_org_404=True`

## M2 Documents and ingestion
- [x] 6. Upload endpoint (`POST /documents`, `GET /documents/{id}`) with type, size and page checks, Storage client, `jobs` module with claim via `SKIP LOCKED`; the `jobs` table itself came with the step 3 schema. First ID-based route, so the cross-org 404 test now runs
- [x] 7. Ingestion worker: parse, chunk, embed, idempotent re-ingest. Worker thread in the API lifespan, attempts-fenced jobs, fake embeddings only (real Gemini is step 9), `faq` jobs fail until step 8 defines their text
- [ ] 8. Document list/delete and FAQ entry endpoints

## M3 Retrieval and ask
- [ ] 9. Gemini provider and `generate` on the `LLMProvider` interface (the interface, `embed` and the fake provider came with step 7)
- [ ] 10. Retrieval, "I don't know" gate, `/ask` with server-verified citations
- [ ] 11. Eval set, injection fixture, threshold tuning

## M4 Ask UI
- [ ] 12. Ask page, citations, Helpful / Not helpful, loading state

## M5 Tickets and triage
- [ ] 13. Ticket create/list, `transition()`
- [ ] 14. Triage and draft job (classify, retrieve, draft)

## M6 Review and hardening
- [ ] 15. Approve/reject/regenerate endpoints and review UI
- [ ] 16. Rate limits (including `/onboarding`: join-code guessing and unlimited org creation), demo notice, hardening

## M7 Ship
- [ ] 17. Seed script, README, deploy, CI polish
