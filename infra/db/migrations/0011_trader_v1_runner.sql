-- Phase 12: collapse the per-loop heartbeat model into a single
-- runner-process model.
--
-- Step 1's `trader_bot_runs` table assumed two separate worker
-- processes (one for ingestion, one for signal+execution) each
-- with its own heartbeat row. Step 12 introduces ONE runner
-- process that orchestrates all six phases (ingest → signal →
-- risk → execute → portfolio_snapshot → dispatch_alerts) per
-- cycle. We add a third allowed value, 'runner', to loop_name.
--
-- The two legacy values stay in the CHECK clause because (a) old
-- rows would otherwise fail constraint validation on ALTER, and
-- (b) keeping them documents the migration history. New rows
-- created by the Phase 12 runner always use 'runner'.
--
-- Idempotent: every `IF EXISTS` / `IF NOT EXISTS` clause is
-- defensive so the worker startup applier can re-run this on
-- every boot without producing a "constraint already exists"
-- error (the schema_migrations table also gates re-application,
-- but defence in depth is cheap).

ALTER TABLE trader_bot_runs
    DROP CONSTRAINT IF EXISTS trader_bot_runs_loop_name_check;

ALTER TABLE trader_bot_runs
    ADD CONSTRAINT trader_bot_runs_loop_name_check
    CHECK (loop_name IN ('ingestion', 'signal_execution', 'runner'));

-- Index for the stale-heartbeat detector: scans rows where
-- status='running' and last_heartbeat_at is older than the
-- threshold. The detector runs every 5 minutes, so this index
-- pays for itself even at very low cardinality.
CREATE INDEX IF NOT EXISTS trader_bot_runs_running_heartbeat_idx
    ON trader_bot_runs (last_heartbeat_at)
    WHERE status = 'running';
