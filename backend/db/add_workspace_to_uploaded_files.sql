alter table uploaded_files add column if not exists workspace_id uuid references workspaces(id) on delete cascade;
create index if not exists uploaded_files_workspace_id_idx on uploaded_files(workspace_id);
