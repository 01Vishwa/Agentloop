-- ============================================================
-- Migration: Add missing foreign-key constraints for eval tables
-- Run this in the Supabase SQL editor ONCE.
-- It is safe to re-run (idempotent via DO / IF NOT EXISTS guards).
-- ============================================================

-- ── eval_metrics.run_id → agent_runs.id ──────────────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_type = 'FOREIGN KEY'
      AND table_name        = 'eval_metrics'
      AND constraint_name   = 'eval_metrics_run_id_fkey'
  ) THEN
    -- Clean up any orphaned records before applying the constraint
    DELETE FROM eval_metrics WHERE run_id NOT IN (SELECT id FROM agent_runs);
    
    ALTER TABLE eval_metrics
      ADD CONSTRAINT eval_metrics_run_id_fkey
      FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE;
  END IF;
END $$;

-- ── eval_steps.run_id → agent_runs.id ────────────────────────────────────────
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.table_constraints
    WHERE constraint_type = 'FOREIGN KEY'
      AND table_name        = 'eval_steps'
      AND constraint_name   = 'eval_steps_run_id_fkey'
  ) THEN
    -- Clean up any orphaned records before applying the constraint
    DELETE FROM eval_steps WHERE run_id NOT IN (SELECT id FROM agent_runs);

    ALTER TABLE eval_steps
      ADD CONSTRAINT eval_steps_run_id_fkey
      FOREIGN KEY (run_id) REFERENCES agent_runs(id) ON DELETE CASCADE;
  END IF;
END $$;

-- Reload PostgREST schema cache immediately so the new FK is visible
-- to the auto-join syntax used by eval_store.list_run_metrics().
NOTIFY pgrst, 'reload schema';
