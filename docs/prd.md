# AI Project Support Desk: Product Requirements (MVP)

Status: Draft, pending approval of the architecture proposal.
Context: Portfolio/demo project. Sample data only. Solo build.

## 1. Summary

A business uploads its docs and FAQs. Logged-in customers ask questions and get answers grounded in those documents (RAG), with links to the sources. When an answer doesn't help, the customer escalates to a ticket. An AI pipeline categorizes the ticket and drafts a reply, and a human agent reviews and approves it before the customer sees it.

## 2. Goals

- G1. Customers get accurate, cited answers drawn only from their business's documents.
- G2. The system admits when it doesn't know instead of guessing.
- G3. Escalating from a failed answer to a ticket is one click and carries full context.
- G4. AI never replies to a customer without human approval.
- G5. Each business's data is isolated from every other business's.
- G6. The whole system runs on free tiers (hosting, database, auth, LLM).

## 3. Users and roles

| Role | Can do |
|---|---|
| Admin | Everything an Agent can do, plus manage documents and view org users |
| Agent | View all org tickets, edit and approve or reject AI drafts |
| Customer | Ask questions, create tickets from answers, view own tickets and approved replies |

Each user belongs to exactly one org. The app is multi-tenant (`org_id` on all tenant data).

## 4. Key decisions

- Customers must log in; there is no anonymous widget.
- Auth is handled by a managed provider (Supabase Auth).
- LLM and embeddings use a free-tier API behind a provider interface.
- The "agent" is a fixed pipeline (classify, retrieve, draft), not an autonomous tool-using agent.
- Approved replies are delivered in-app only (no email).
- Both the AI draft and the human-approved final text are stored.
- Stack: React + TypeScript (web), Python/FastAPI (API), Supabase Postgres + pgvector.
- Cold starts on free hosting are accepted, with a loading state in the UI.

## 5. User flows

### F1. Onboarding and login
1. A user signs up or logs in (email/password, Google optional).
2. A new user is attached to an org with a role (first user of a new org is Admin; others are invited or assigned).
3. The user lands on a role-appropriate home: Admin on Documents, Agent on the ticket queue, Customer on Ask.

### F2. Document upload (Admin)
1. Admin selects PDF, Markdown or TXT files, or enters a Q/A pair as a pasted FAQ entry.
2. The file is stored and a document row appears with status `queued`.
3. A background job parses, chunks and embeds it. Status moves to `processing`, then `ready` or `failed` (with a reason).
4. Admin can delete a document, which removes its file, chunks and embeddings.

### F3. Ask a question (Customer)
1. Customer types a question.
2. The system retrieves the most relevant chunks from the customer's org only.
3. If relevance is sufficient, it returns an answer with citations (document name, page or section, snippet, link to the source).
4. If relevance is insufficient, it replies that it doesn't have the information and offers to create a ticket.
5. The customer marks the answer Helpful or Not helpful.

### F4. Escalate to ticket (Customer)
1. On "Not helpful" or "I don't know", the customer clicks "Create ticket".
2. A ticket is created pre-filled with the question, the AI answer (if any) and the retrieved sources. The customer can add details.
3. The ticket appears in the customer's ticket list with status `open`.

### F5. AI triage and draft (system)
1. Ticket creation enqueues a job.
2. The job assigns a category and priority, retrieves relevant chunks, and generates a draft reply with citations.
3. The draft is stored as `ai_text`. The ticket moves to `awaiting_review`.
4. On failure, the ticket still appears in the queue as `needs_manual_reply` with no draft.

### F6. Review and approve (Agent/Admin)
1. The agent opens a ticket and sees the question, context, category, priority, and the draft with its sources.
2. The agent edits the draft if needed, then approves or rejects it.
3. On approve, the final text is stored as `final_text` with the approver and time. The ticket moves to `answered` and the customer sees the reply on the ticket page.
4. On reject, the agent can regenerate the draft or write a reply manually.

## 6. Acceptance criteria

Each criterion is testable by an automated test or a scripted manual check.

### Auth and tenancy
- AC-1. A user can sign up, log in and log out. An unauthenticated request to any non-public API endpoint returns 401.
- AC-2. Every user has exactly one org and one role (Admin, Agent or Customer), carried in the verified JWT or looked up server-side.
- AC-3. A Customer calling an Admin-only or Agent-only endpoint receives 403.
- AC-4. Tenant isolation: with data seeded in org A and org B, no endpoint (documents, ask, tickets, drafts) called as an org A user ever returns, cites or retrieves any org B record, including when guessing org B IDs (returns 404).
- AC-5. Retrieval queries always include an `org_id` filter derived from the authenticated user, never from request input.

### Document ingestion
- AC-6. Admin can upload `.pdf`, `.md` and `.txt` files. Other types are rejected with a clear error.
- AC-7. Files over 10 MB or PDFs over 100 pages are rejected before processing, with a message stating the limit.
- AC-8. A valid text-based PDF reaches status `ready` and produces at least one chunk. Each chunk stores its document, org, and page or position.
- AC-9. A corrupt or scanned (no extractable text) PDF ends in status `failed` with a human-readable reason, and does not block other jobs.
- AC-10. The document list shows live status (`queued`, `processing`, `ready`, `failed`) without requiring a manual page reload beyond polling or refresh.
- AC-11. Re-running an ingestion job for the same document does not create duplicate chunks (idempotent).
- AC-12. Deleting a document removes its stored file, chunks and embeddings. After deletion, its content is never retrieved or cited.
- AC-13. Admin can add a FAQ entry (question and answer text). It becomes retrievable like any other document.
- AC-14. Only Admin can upload or delete documents.

### Question answering
- AC-15. For each question in a seeded eval set where the answer exists in the documents, the answer contains at least one citation pointing to a chunk that contains the supporting text. Target: at least 80% of eval questions answered correctly (human-graded on a 20–30 question set).
- AC-16. Every citation in a response references a real chunk from the asker's org. Citation IDs not in the retrieved set are dropped or the response is rejected.
- AC-17. Each citation shows document name, page or section, and a snippet, and links to the source document.
- AC-18. For a question with no supporting content (eval set of out-of-scope questions), the system returns the "I don't know" response with no fabricated answer, in at least 90% of eval cases.
- AC-19. The answer is generated only from retrieved chunks. The prompt includes no data from other orgs.
- AC-20. A customer can mark an answer Helpful or Not helpful. The choice is stored with the question.
- AC-21. If the LLM provider errors or is rate-limited, the user sees a retryable error message. The app does not crash and no partial answer is stored as final.
- AC-22. During a cold start or a response taking over 3 seconds, the UI shows a loading state.
- AC-23. Per-user rate limit on the ask endpoint (default 20 requests per minute). Exceeding it returns 429 with a message.

### Tickets
- AC-24. A Customer can create a ticket from a "Not helpful" or "I don't know" answer. The ticket stores the question, the AI answer, the retrieved chunk IDs and the customer's added notes.
- AC-25. A Customer sees only their own tickets. Agent and Admin see all tickets in their org.
- AC-26. Ticket status is one of: `open`, `awaiting_review`, `needs_manual_reply`, `answered`, `closed`. Invalid transitions are rejected.
- AC-27. The ticket list can be filtered by status and category for Agent and Admin.

### AI triage and drafting
- AC-28. Within 60 seconds of ticket creation (when the provider is available), the ticket has a category and priority, and the status is `awaiting_review` with an `ai_text` draft.
- AC-29. Category is one of a fixed list (Billing, Technical, Account, Other). Priority is one of Low, Medium, High. Output that fails schema validation is retried once, then falls back to category Other and priority Medium.
- AC-30. The draft cites its sources using the same citation rules as AC-16 and AC-17.
- AC-31. If the pipeline fails after retries, the ticket moves to `needs_manual_reply` and is still visible in the agent queue.
- AC-32. The pipeline job is idempotent: running it twice for one ticket produces one active draft.

### Human approval
- AC-33. The customer cannot see any draft text until it is approved. No API response to a Customer includes `ai_text` of an unapproved draft.
- AC-34. An Agent or Admin can edit a draft, then approve or reject it. Approval records `final_text`, approver ID and timestamp, and moves the ticket to `answered`.
- AC-35. Both `ai_text` and `final_text` are retained after approval. The original `ai_text` is never overwritten.
- AC-36. Rejecting a draft allows regenerating it or writing a manual reply. A manual reply follows the same approval record rules.
- AC-37. There is no code path that delivers a reply to a customer without an approval record.

### Security and robustness
- AC-38. Uploaded document text and ticket text are treated as untrusted. The LLM calls in the pipeline have no tool access and no write access. A test document containing "ignore previous instructions and ..." does not change the response format, leak other orgs' data, or bypass the "I don't know" path.
- AC-39. The LLM API key and Supabase service key never appear in frontend bundles or API responses.
- AC-40. The UI shows a notice that the app is a demo and users should not upload sensitive data.

### Delivery
- AC-41. Web and API are deployed to free-tier hosts and reachable by URL, with a README documenting setup, env vars and the demo flow.
- AC-42. CI runs lint, backend tests and the frontend build on every push. LLM calls are mocked in CI.
- AC-43. A seeded demo org with sample documents and an eval set is available through a documented script.

## 7. Out of scope (MVP)

- Email or Slack inbound and outbound, and third-party helpdesk integrations (Zendesk, Intercom and similar).
- Anonymous or public embeddable chat widget.
- Auto-sending replies without human approval, or any autonomous tool-using agent.
- OCR for scanned PDFs, DOCX, HTML and URL crawling.
- Billing, subscriptions and plan limits.
- Multilingual support and voice.
- Analytics dashboards and SLA tracking.
- Per-document permissions or visibility levels (one visibility level per org).
- Admin-defined ticket categories (fixed list for MVP).
- SSO beyond Google, and organization invitations beyond a basic assignment flow.
- Real or sensitive customer data, and compliance work (GDPR, SOC 2, HIPAA).
- Always-on hosting guarantees.

## 8. Open items

- Embedding model and vector dimension (confirm against current provider docs before schema work).
- Whether to enable Supabase row-level security in addition to API-level scoping (recommended).
- Weekly time budget and target dates for milestones.
- Exact thresholds (similarity cutoff, file limits, rate limits) to be tuned during M3 and M6 using the eval set.
