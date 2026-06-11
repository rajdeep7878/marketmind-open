-- Trader v1: ops — drift metrics, bot heartbeat, audit log.
--
-- Drift metrics compare paper performance against the backtest_metrics
-- snapshot frozen on `trader_strategy_versions`. Computed daily by
-- the drift job. The reference (backtest) numbers are denormalised
-- onto each row so a historical drift query doesn't have to re-parse
-- the version row's JSONB blob — and so the comparison stays valid
-- even if the version's backtest_metrics ever gets adjusted (which
-- the immutability trigger forbids, but the redundancy is cheap
-- insurance).
--
-- `trader_bot_runs` is the per-loop heartbeat. A row is inserted at
-- loop start; `last_heartbeat_at` is touched every iteration. The
-- stale-heartbeat detector runs periodically: rows whose last
-- heartbeat is older than threshold get status='crashed' and an
-- alert dispatched. Graceful shutdown (SIGTERM) writes 'stopped'.
--
-- `trader_audit_logs` is the structured append-only event log. Every
-- state-changing actor (ingestion_loop, signal_engine, risk_manager,
-- executor, portfolio, drift, alerts) writes here. Distinct from
-- structlog output: structlog goes to stdout for aggregators; this
-- table is for in-app queries ("give me everything that happened
-- around 2026-05-18T14:30Z") and survives log-pipeline outages.

CREATE TABLE IF NOT EXISTS trader_drift_metrics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy_version_id UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    -- '7d', '30d', 'all' — free-form rather than enum so the drift
    -- job can experiment with new windows without a migration.
    -- Column name carries the `_label` suffix because `window` is a
    -- Postgres reserved word (used by window functions) and an
    -- unquoted reference would be a parse error in every future query.
    window_label TEXT NOT NULL,
    paper_trade_count INTEGER NOT NULL,
    paper_win_rate NUMERIC NOT NULL,
    paper_avg_return_per_trade NUMERIC NOT NULL,
    paper_current_drawdown_pct NUMERIC NOT NULL,
    backtest_trade_freq_per_week NUMERIC NOT NULL,
    backtest_win_rate NUMERIC NOT NULL,
    backtest_avg_return_per_trade NUMERIC NOT NULL,
    backtest_max_drawdown_pct NUMERIC NOT NULL,
    trade_freq_ratio NUMERIC NOT NULL,
    win_rate_delta NUMERIC NOT NULL,
    avg_return_delta NUMERIC NOT NULL,
    drawdown_ratio NUMERIC NOT NULL,
    health_status TEXT NOT NULL CHECK (health_status IN ('healthy', 'watch', 'breach'))
);

CREATE INDEX IF NOT EXISTS trader_drift_metrics_strategy_idx
    ON trader_drift_metrics (strategy_version_id, ts DESC);


CREATE TABLE IF NOT EXISTS trader_bot_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    loop_name TEXT NOT NULL CHECK (loop_name IN ('ingestion', 'signal_execution')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL CHECK (status IN ('running', 'stopped', 'crashed')),
    -- RQ worker name (hostname:pid:seq). Stored verbatim so a forensic
    -- query can match a crashed loop back to a specific worker process
    -- in the container logs.
    worker_id TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS trader_bot_runs_status_idx
    ON trader_bot_runs (loop_name, status);
CREATE INDEX IF NOT EXISTS trader_bot_runs_heartbeat_idx
    ON trader_bot_runs (last_heartbeat_at)
    WHERE status = 'running';


CREATE TABLE IF NOT EXISTS trader_audit_logs (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    -- TEXT (not UUID) because some entities are addressed by composite
    -- keys (e.g., '(symbol=BTC/USDT, timeframe=4h, close_ts=...)') or
    -- by integer ids (candles).
    entity_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS trader_audit_logs_ts_idx
    ON trader_audit_logs (ts DESC);
CREATE INDEX IF NOT EXISTS trader_audit_logs_entity_idx
    ON trader_audit_logs (entity_type, entity_id);
