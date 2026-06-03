-- Migration 003: workspace_files table
-- Enables multi-file workspace joins by storing per-file metadata alongside
-- the workspace session.  Each row represents one uploaded file associated
-- with a workspace, including its parsed schema (columns + dtypes + samples)
-- so the schema_merger can infer join keys without re-reading the files.
--
-- Backward-compat: A backfill INSERT creates one row (upload_order=1) for every
-- existing workspace that already stores a file_path column, preserving all
-- prior sessions without disruption.

-- ---------------------------------------------------------------------------
-- Table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workspace_files (
    id            uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    workspace_id  uuid        NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       uuid        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    file_name     text        NOT NULL,
    file_path     text        NOT NULL,
    file_type     text        NOT NULL
                              CHECK (file_type IN ('csv', 'xlsx', 'parquet', 'md', 'json')),
    row_count     int,
    schema_json   jsonb       NOT NULL DEFAULT '{}',
    upload_order  int         NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Row-Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE workspace_files ENABLE ROW LEVEL SECURITY;

-- Users can read their own files only.
CREATE POLICY workspace_files_select
    ON workspace_files
    FOR SELECT
    USING (user_id = auth.uid());

-- Users can insert their own files only.
CREATE POLICY workspace_files_insert
    ON workspace_files
    FOR INSERT
    WITH CHECK (user_id = auth.uid());

-- Users can delete their own files only.
CREATE POLICY workspace_files_delete
    ON workspace_files
    FOR DELETE
    USING (user_id = auth.uid());

-- ---------------------------------------------------------------------------
-- Index
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_workspace_files_lookup
    ON workspace_files (workspace_id, user_id, upload_order ASC);

-- ---------------------------------------------------------------------------
-- Backfill
-- Inserts one workspace_files row (upload_order=1) for each existing workspace
-- row that has a non-null file_path column.  This keeps the schema_merger
-- aware of legacy single-file sessions without any manual intervention.
--
-- If the workspaces table has no file_path column this statement is a no-op
-- because the SELECT returns no rows (column reference would fail at parse
-- time -- wrap in a DO block so it degrades gracefully).
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    -- Only run if the workspaces table has a file_path column (legacy schema).
    IF EXISTS (
        SELECT 1
        FROM   information_schema.columns
        WHERE  table_name  = 'workspaces'
        AND    column_name = 'file_path'
    ) THEN
        INSERT INTO workspace_files (
            workspace_id,
            user_id,
            file_name,
            file_path,
            file_type,
            row_count,
            schema_json,
            upload_order,
            created_at
        )
        SELECT
            w.id                                    AS workspace_id,
            w.user_id                               AS user_id,
            COALESCE(
                substring(w.file_path FROM '[^/]+$'),
                'unknown'
            )                                       AS file_name,
            w.file_path                             AS file_path,
            -- Best-effort file type from extension; default to 'csv'.
            LOWER(COALESCE(
                substring(w.file_path FROM '\.([^.]+)$'),
                'csv'
            ))                                      AS file_type,
            NULL                                    AS row_count,
            '{}'::jsonb                             AS schema_json,
            1                                       AS upload_order,
            w.created_at                            AS created_at
        FROM   workspaces w
        WHERE  w.file_path IS NOT NULL
        -- Skip workspaces that already have a workspace_files row.
        AND    NOT EXISTS (
            SELECT 1
            FROM   workspace_files wf
            WHERE  wf.workspace_id = w.id
        );

        RAISE NOTICE 'workspace_files backfill complete.';
    ELSE
        RAISE NOTICE 'workspaces.file_path column not present — backfill skipped (no-op).';
    END IF;
END;
$$;
