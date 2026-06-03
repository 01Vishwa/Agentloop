-- Migration 004: workspace_files improvements
--
-- 1. Add file_size column to track uploaded file size in bytes so the
--    frontend can display it without fetching file content.
-- 2. Widen the file_type CHECK constraint to include 'txt' and 'pdf'
--    which are accepted by the upload UI but were missing from the
--    original constraint, causing silent insert failures.

-- Add file_size column (nullable for backward compat with existing rows)
ALTER TABLE workspace_files
    ADD COLUMN IF NOT EXISTS file_size bigint;

COMMENT ON COLUMN workspace_files.file_size
    IS 'File size in bytes, populated at upload time.';

-- Drop the old restrictive CHECK and create a wider one
ALTER TABLE workspace_files
    DROP CONSTRAINT IF EXISTS workspace_files_file_type_check;

ALTER TABLE workspace_files
    ADD CONSTRAINT workspace_files_file_type_check
    CHECK (file_type IN ('csv', 'xlsx', 'xls', 'parquet', 'md', 'json', 'txt', 'pdf'));
