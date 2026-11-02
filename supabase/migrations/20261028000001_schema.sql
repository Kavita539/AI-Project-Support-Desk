-- M1 step 2: core schema. RLS policies come in a separate migration.
create extension if not exists vector;

create table orgs (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  created_at timestamptz not null default now()
);

create table profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  org_id uuid not null references orgs(id),
  role text not null check (role in ('admin', 'agent', 'customer')),
  email text not null,
  created_at timestamptz not null default now()
);
create index profiles_org_id_idx on profiles (org_id);

create table documents (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  name text not null,
  kind text not null check (kind in ('pdf', 'md', 'txt', 'faq')),
  storage_path text,
  status text not null default 'queued'
    check (status in ('queued', 'processing', 'ready', 'failed')),
  error text,
  page_count int,
  size_bytes bigint,
  created_by uuid references profiles(user_id),
  created_at timestamptz not null default now()
);
create index documents_org_id_idx on documents (org_id);

create table chunks (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  document_id uuid not null references documents(id) on delete cascade,
  content text not null,
  embedding vector(768),
  page int,
  position int not null,
  section text,
  created_at timestamptz not null default now(),
  unique (document_id, position)
);
create index chunks_org_document_idx on chunks (org_id, document_id);
create index chunks_embedding_idx on chunks using hnsw (embedding vector_cosine_ops);

create table questions (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  user_id uuid not null references profiles(user_id),
  query text not null,
  answer text,
  status text not null check (status in ('answered', 'no_answer', 'error')),
  retrieved_chunk_ids uuid[] not null default '{}',
  citations jsonb not null default '[]',
  top_score real,
  helpful boolean,
  created_at timestamptz not null default now()
);
create index questions_org_id_idx on questions (org_id);

create table tickets (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  customer_id uuid not null references profiles(user_id),
  question_id uuid references questions(id),
  subject text not null,
  body text not null,
  status text not null default 'open'
    check (status in ('open', 'awaiting_review', 'needs_manual_reply', 'answered', 'closed')),
  category text check (category in ('Billing', 'Technical', 'Account', 'Other')),
  priority text check (priority in ('Low', 'Medium', 'High')),
  ai_answer text,
  retrieved_chunk_ids uuid[] not null default '{}',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index tickets_org_status_idx on tickets (org_id, status);
create index tickets_customer_idx on tickets (org_id, customer_id);

create table ticket_messages (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  ticket_id uuid not null references tickets(id) on delete cascade,
  author_id uuid not null references profiles(user_id),
  author_role text not null check (author_role in ('admin', 'agent', 'customer')),
  body text not null,
  created_at timestamptz not null default now()
);
create index ticket_messages_org_ticket_idx on ticket_messages (org_id, ticket_id);

create table drafts (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  ticket_id uuid not null references tickets(id) on delete cascade,
  ai_text text,
  citations jsonb not null default '[]',
  source text not null check (source in ('ai', 'manual')),
  working_text text,
  final_text text,
  approved_by uuid references profiles(user_id),
  approved_at timestamptz,
  state text not null default 'pending' check (state in ('pending', 'approved', 'rejected')),
  created_at timestamptz not null default now(),
  check (state <> 'approved'
    or (final_text is not null and approved_by is not null and approved_at is not null))
);
create index drafts_org_ticket_idx on drafts (org_id, ticket_id);
create unique index drafts_one_pending_idx on drafts (ticket_id) where state = 'pending';

create table jobs (
  id uuid primary key default gen_random_uuid(),
  org_id uuid not null references orgs(id),
  type text not null check (type in ('ingest_document', 'triage_ticket')),
  payload jsonb not null default '{}',
  status text not null default 'queued' check (status in ('queued', 'running', 'done', 'failed')),
  attempts int not null default 0,
  max_attempts int not null default 3,
  run_after timestamptz not null default now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now()
);
create index jobs_claim_idx on jobs (status, run_after);
create index jobs_org_id_idx on jobs (org_id);
