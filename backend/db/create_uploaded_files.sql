-- ============================================================
-- Uploaded Files Schema — Supabase SQL Migration
-- Run the FULL contents of this file in the Supabase SQL editor.
-- It is safe to re-run on an existing database (idempotent).
-- ============================================================

create table if not exists uploaded_files (
  id             uuid        primary key default gen_random_uuid(),
  filename       text        not null,
  file_path      text,
  file_url       text,
  file_size      int,
  extension      text,
  user_id        uuid        references auth.users(id) on delete set null,
  workspace_id   uuid        references workspaces(id) on delete cascade,
  created_at     timestamptz not null default now()
);

alter table uploaded_files enable row level security;

drop policy if exists "Users access own uploaded_files" on uploaded_files;

create policy "Users access own uploaded_files"
  on uploaded_files
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index if not exists uploaded_files_workspace_id_idx on uploaded_files(workspace_id);
create index if not exists uploaded_files_user_id_idx on uploaded_files(user_id);
