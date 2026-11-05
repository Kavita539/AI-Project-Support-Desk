-- M1 step 4b: join codes for onboarding. Kept out of `orgs` because the orgs RLS policy
-- lets every member (including customers) read their own org row via supabase-js.
-- RLS is on with no policies, so only the API (service role) can read or write codes.
create table org_join_codes (
  org_id uuid primary key references orgs(id) on delete cascade,
  code text not null unique,
  created_at timestamptz not null default now()
);

alter table org_join_codes enable row level security;
