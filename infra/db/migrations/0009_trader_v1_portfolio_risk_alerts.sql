-- Trader v1: portfolio snapshots, risk events, alerts.
--
-- One portfolio snapshot per signal-execution cycle. The equity curve
-- and drawdown are tracked here rather than derived on demand because
-- the loop's `peak_equity` must remain stable across restarts.
-- Re-walking historical fills on every cycle to recompute peak would
-- both be expensive and could differ run-to-run if a closed fill
-- ever got soft-deleted or corrected — anchoring peak on a persisted
-- column makes the drawdown definition stable.
--
-- Risk events are the audit trail of every block decision and every
-- detection (kill-switch tripping, daily/weekly loss breach, stale
-- data, etc.). The risk manager writes one row per block in the same
-- transaction that produces the alert; the executor never proceeds
-- on a blocked signal unless the row was committed. signal_id is
-- nullable because some events (kill_switch, stale_data) are not
-- tied to a specific signal.
--
-- Alerts have a DB row even when network delivery (Telegram) fails:
-- the row is the source of truth for the GET /trader/alerts/recent
-- API. `delivered` + `delivery_error` are operational metadata, not
-- functional state.

CREATE TABLE IF NOT EXISTS trader_portfolio_snapshots (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cash NUMERIC NOT NULL,
    equity NUMERIC NOT NULL,
    unrealised_pnl NUMERIC NOT NULL,
    realised_pnl_cumulative NUMERIC NOT NULL,
    peak_equity NUMERIC NOT NULL,
    drawdown NUMERIC NOT NULL,
    drawdown_pct NUMERIC NOT NULL,
    open_positions_count INTEGER NOT NULL,
    per_strategy_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb,
    per_symbol_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS trader_portfolio_snapshots_ts_idx
    ON trader_portfolio_snapshots (ts DESC);


CREATE TABLE IF NOT EXISTS trader_risk_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'block',
        'kill_switch',
        'daily_loss_breach',
        'weekly_loss_breach',
        'stale_data',
        'volatility_regime',
        'strategy_disabled',
        'strategy_not_paper_approved',
        'drift_breach'
    )),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    -- SET NULL on FK delete: keeping the risk event after the strategy
    -- version is gone is more useful than cascading the row away (the
    -- audit trail outlives the entity it was about).
    strategy_version_id UUID
        REFERENCES trader_strategy_versions (id) ON DELETE SET NULL,
    symbol TEXT,
    signal_id UUID
        REFERENCES trader_signals (id) ON DELETE SET NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS trader_risk_events_recent_idx
    ON trader_risk_events (ts DESC);
CREATE INDEX IF NOT EXISTS trader_risk_events_strategy_idx
    ON trader_risk_events (strategy_version_id, ts DESC)
    WHERE strategy_version_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS trader_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    channel TEXT NOT NULL CHECK (channel IN ('telegram', 'log')),
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    delivered BOOLEAN NOT NULL DEFAULT FALSE,
    delivery_error TEXT
);

CREATE INDEX IF NOT EXISTS trader_alerts_recent_idx
    ON trader_alerts (ts DESC);
