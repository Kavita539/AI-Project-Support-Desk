-- M1 step 4b hardening (from code review). Defense in depth for org_join_codes:
-- only well-formed codes can be stored, and the anon/authenticated roles have no
-- privileges on the table at all (RLS with no policies already blocks them).
-- The pattern matches the API alphabet: A-Z without I and O, digits 2-9.
alter table org_join_codes
  add constraint org_join_codes_code_format check (code ~ '^[A-HJ-NP-Z2-9]{8}$');

revoke all on org_join_codes from anon, authenticated;
