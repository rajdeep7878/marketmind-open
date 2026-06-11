-- Phase 4: overfitting analyses.
--
-- One row per backtest_result. The analysis is expensive (~2 minutes
-- for a 6-window walk-forward + 25-cell sweep + 100 Monte Carlo
-- permutations + deflated Sharpe), so we make it idempotent via a
-- UNIQUE index on backtest_id: re-running on the same backtest
-- short-circuits to the existing row.
--
-- The five sub-analyses are stored as separate JSONB columns so a
-- future query can hit any one of them without parsing the whole
-- blob. The composite score lives in its own column for the same
-- reason — listings and dashboards want the score number without
-- pulling the full analysis.

CREATE TABLE IF NOT EXISTS overfitting_analyses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    backtest_id UUID NOT NULL REFERENCES backtest_results (id) ON DELETE CASCADE,
    walk_forward_json JSONB NOT NULL,
    parameter_sweep_json JSONB NOT NULL,
    monte_carlo_json JSONB NOT NULL,
    deflated_sharpe_json JSONB NOT NULL,
    composite_score_json JSONB NOT NULL,
    compute_seconds DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS overfitting_analyses_backtest_idx
    ON overfitting_analyses (backtest_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS overfitting_analyses_backtest_unique_idx
    ON overfitting_analyses (backtest_id);
