-- M2 step 6: private Storage bucket for uploaded documents.
-- Files live at {org_id}/{document_id}/{filename}. The API (service role) is the only
-- caller: storage.objects has RLS on and this project adds no policies for anon or
-- authenticated, so browsers cannot read or write the bucket directly.
-- The limits repeat the API checks (10 MB; pdf, markdown, plain text) as a backstop.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'documents',
  'documents',
  false,
  10485760,
  array['application/pdf', 'text/markdown', 'text/plain']
)
on conflict (id) do nothing;
