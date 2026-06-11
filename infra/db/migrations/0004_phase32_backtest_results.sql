-- Phase 3.2: backtest results.
--
-- One row per (strategy, start, end, initial_capital) tuple. The
-- unique index lets POST /strategies/{id}/backtest be safely
-- idempotent: a repeat call with identical params returns the existing
-- row instead of running the engine a second time.
--
-- The full BacktestResult (spec_snapshot + run with equity curve +
-- metrics + benchmark + author comparisons + timings) is stored as
-- JSONB. The equity curve embedded in result_json is at full
-- resolution; the API downsamples for the list view but keeps the full
-- curve available via GET /backtests/{id} so the per-run page can
-- offer to download the raw timeline.

CREATE TABLE IF NOT EXISTS backtest_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID NOT NULL REFERENCES extracted_strategies (id) ON DELETE CASCADE,
    start_ts TIMESTAMPTZ NOT NULL,
    end_ts TIMESTAMPTZ NOT NULL,
    initial_capital DOUBLE PRECISION NOT NULL,
    result_json JSONB NOT NULL,
    data_fetch_seconds DOUBLE PRECISION NOT NULL,
    compute_seconds DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS backtest_results_strategy_idx
    ON backtest_results (strategy_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS backtest_results_idempotency_idx
    ON backtest_results (strategy_id, start_ts, end_ts, initial_capital);
