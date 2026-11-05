-- Stand-ins for what Supabase provides, so supabase/migrations/*.sql apply to a plain
-- pgvector Postgres (CI service container). Test-only; never run against Supabase.

create extension if not exists vector;
create extension if not exists pgcrypto;

do $$
begin
  if not exists (select from pg_roles where rolname = 'anon') then
    create role anon nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'service_role') then
    create role service_role nologin bypassrls;
  end if;
end
$$;

create schema if not exists auth;

create table if not exists auth.users (
  id uuid primary key,
  email text
);

create or replace function auth.uid() returns uuid
  language sql stable
  as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;

grant usage on schema public, auth to anon, authenticated, service_role;
-- Like Supabase, new public tables are readable by the API roles by default; the
-- hardening migration revokes this for org_join_codes, which is what we test.
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
