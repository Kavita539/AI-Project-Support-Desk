-- M1 step 3: RLS as a backstop. The API uses the service role (bypasses RLS) and
-- scopes by org_id itself. These policies limit what anon-key clients can read via
-- supabase-js. No insert/update/delete policies: all writes go through the API.

-- Helpers read profiles as definer so policies on profiles do not recurse.
create function auth_org_id() returns uuid
  language sql stable security definer set search_path = public
  as $$ select org_id from profiles where user_id = auth.uid() $$;

create function auth_role() returns text
  language sql stable security definer set search_path = public
  as $$ select role from profiles where user_id = auth.uid() $$;

alter table orgs enable row level security;
alter table profiles enable row level security;
alter table documents enable row level security;
alter table chunks enable row level security;
alter table questions enable row level security;
alter table tickets enable row level security;
alter table ticket_messages enable row level security;
alter table drafts enable row level security;
alter table jobs enable row level security;

create policy orgs_select on orgs for select to authenticated
  using (id = auth_org_id());

create policy profiles_select on profiles for select to authenticated
  using (user_id = auth.uid() or (org_id = auth_org_id() and auth_role() = 'admin'));

create policy documents_select on documents for select to authenticated
  using (org_id = auth_org_id());

create policy chunks_select on chunks for select to authenticated
  using (org_id = auth_org_id());

create policy questions_select on questions for select to authenticated
  using (org_id = auth_org_id()
    and (user_id = auth.uid() or auth_role() in ('admin', 'agent')));

create policy tickets_select on tickets for select to authenticated
  using (org_id = auth_org_id()
    and (customer_id = auth.uid() or auth_role() in ('admin', 'agent')));

create policy ticket_messages_select on ticket_messages for select to authenticated
  using (org_id = auth_org_id()
    and (auth_role() in ('admin', 'agent')
      or exists (select 1 from tickets t
                 where t.id = ticket_id and t.customer_id = auth.uid())));

-- Drafts hold ai_text, which customers must never see (they read approved
-- replies through the API serializer). Staff only.
create policy drafts_select on drafts for select to authenticated
  using (org_id = auth_org_id() and auth_role() in ('admin', 'agent'));

-- jobs: RLS on, no policies, so only the service role can touch it.
