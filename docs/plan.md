# Build Plan

Milestones follow `docs/design.md` section 9. Tick a step only when lint, typecheck and tests pass.

## M0 Scaffold
- [x] 1. Repo scaffold: `apps/web`, `apps/api`, `supabase/`, CI workflow, `.env.example`, CLAUDE.md

## M1 Schema, auth, tenancy
- [x] 2. Migrations: orgs, profiles, documents, chunks (`vector(768)`), tickets, drafts, jobs, indexes
- [ ] 3. RLS policies as backstop
- [ ] 4. JWT verification, `profiles` lookup, `GET /api/v1/me`
- [ ] 5. Tenancy tests (cross-org 404, wrong role 403, no token 401)

## M2 Documents and ingestion
- [ ] 6. Upload endpoint with type and size checks, `jobs` table, claim with `SKIP LOCKED`
- [ ] 7. Ingestion worker: parse, chunk, embed, idempotent re-ingest
- [ ] 8. Document list/delete and FAQ entry endpoints

## M3 Retrieval and ask
- [ ] 9. `LLMProvider` interface, Gemini and fake providers
- [ ] 10. Retrieval, "I don't know" gate, `/ask` with server-verified citations
- [ ] 11. Eval set, injection fixture, threshold tuning

## M4 Ask UI
- [ ] 12. Ask page, citations, Helpful / Not helpful, loading state

## M5 Tickets and triage
- [ ] 13. Ticket create/list, `transition()`
- [ ] 14. Triage and draft job (classify, retrieve, draft)

## M6 Review and hardening
- [ ] 15. Approve/reject/regenerate endpoints and review UI
- [ ] 16. Rate limits, demo notice, hardening

## M7 Ship
- [ ] 17. Seed script, README, deploy, CI polish
