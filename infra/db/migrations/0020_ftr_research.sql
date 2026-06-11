-- 0019: FTR (Frequent-Trading Research) — Phase D paper-only research module.
-- All tables are new and self-contained: no foreign keys into existing
-- tables, fully detachable. Money columns are NUMERIC (Decimal in Python).

-- Full DecisionRecord stream, skips included. The idempotency key
-- (strategy_id, symbol, bar_ts) makes restarts crash-safe: re-evaluating a
-- bar after a restart is a no-op.
CREATE TABLE IF NOT EXISTS ftr_decisions (
    id              BIGSERIAL PRIMARY KEY,
    ts_utc          TIMESTAMPTZ NOT NULL,
    strategy_id     TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    bar_ts          TIMESTAMPTZ NOT NULL,
    action          TEXT        NOT NULL CHECK (action IN ('ENTER_LONG','EXIT','HOLD','SKIP')),
    qty             NUMERIC(24, 12) NOT NULL DEFAULT 0,
    expected_move_bps   DOUBLE PRECISION,
    expected_cost_bps   DOUBLE PRECISION,
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0,
    reason_codes    JSONB       NOT NULL,
    feature_snapshot_hash TEXT  NOT NULL DEFAULT '',
    model_version   TEXT        NOT NULL DEFAULT '',
    git_sha         TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_id, symbol, bar_ts)
);
CREATE INDEX IF NOT EXISTS idx_ftr_decisions_strategy_ts
    ON ftr_decisions (strategy_id, ts_utc DESC);

CREATE TABLE IF NOT EXISTS ftr_orders (
    id              BIGSERIAL PRIMARY KEY,
    decision_id     BIGINT REFERENCES ftr_decisions(id),
    ts_utc          TIMESTAMPTZ NOT NULL,
    strategy_id     TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    side            TEXT        NOT NULL CHECK (side IN ('buy','sell')),
    qty             NUMERIC(24, 12) NOT NULL,
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','filled','rejected','cancelled')),
    venue_profile   TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ftr_fills (
    id              BIGSERIAL PRIMARY KEY,
    order_id        BIGINT NOT NULL REFERENCES ftr_orders(id),
    ts_utc          TIMESTAMPTZ NOT NULL,
    symbol          TEXT        NOT NULL,
    side            TEXT        NOT NULL,
    qty             NUMERIC(24, 12) NOT NULL,
    reference_price NUMERIC(24, 12) NOT NULL,   -- 1m close used as reference
    fill_price      NUMERIC(24, 12) NOT NULL,   -- worsened by half-spread+slippage
    fee_paid        NUMERIC(24, 12) NOT NULL,
    venue_profile   TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ftr_positions (
    id              BIGSERIAL PRIMARY KEY,
    strategy_id     TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    qty             NUMERIC(24, 12) NOT NULL,
    avg_entry_price NUMERIC(24, 12) NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    UNIQUE (strategy_id, symbol, opened_at)
);
CREATE INDEX IF NOT EXISTS idx_ftr_positions_open
    ON ftr_positions (strategy_id, symbol) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS ftr_equity_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ts_utc          TIMESTAMPTZ NOT NULL,
    cash            NUMERIC(24, 12) NOT NULL,
    positions_value NUMERIC(24, 12) NOT NULL,
    equity          NUMERIC(24, 12) NOT NULL,
    gross_exposure_pct  DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE (ts_utc)
);

CREATE TABLE IF NOT EXISTS ftr_model_registry (
    id              BIGSERIAL PRIMARY KEY,
    model_version   TEXT        NOT NULL UNIQUE,
    model_family    TEXT        NOT NULL,
    train_window_start  TIMESTAMPTZ,
    train_window_end    TIMESTAMPTZ,
    feature_config_hash TEXT    NOT NULL DEFAULT '',
    artifact_hash   TEXT        NOT NULL,
    artifact_path   TEXT,
    git_sha         TEXT        NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ftr_data_quality (
    id              BIGSERIAL PRIMARY KEY,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    exchange        TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    timeframe       TEXT        NOT NULL,
    rows            BIGINT      NOT NULL,
    first_ts        TIMESTAMPTZ,
    last_ts         TIMESTAMPTZ,
    passed          BOOLEAN     NOT NULL,
    details         JSONB       NOT NULL
);

CREATE TABLE IF NOT EXISTS ftr_verdicts (
    id              BIGSERIAL PRIMARY KEY,
    run_stamp       TEXT        NOT NULL,
    strategy_id     TEXT        NOT NULL,
    venue_profile   TEXT        NOT NULL,
    uk_execution_feasible BOOLEAN NOT NULL,
    verdict         TEXT        NOT NULL CHECK (verdict IN
                        ('PASS','PASS_LOW_FREQUENCY',
                         'CONDITIONAL_PASS_INFEASIBLE_VENUE',
                         'REJECTED','INSUFFICIENT_DATA')),
    failed_gates    JSONB       NOT NULL DEFAULT '[]',
    n_trials        INTEGER     NOT NULL DEFAULT 0,
    metrics         JSONB       NOT NULL DEFAULT '{}',
    artifact_path   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_stamp, strategy_id, venue_profile)
);

-- Kill switch: single-row flag table; the trader halts when engaged.
-- Reset requires a manual UPDATE (or the KILLSWITCH file alternative).
CREATE TABLE IF NOT EXISTS ftr_killswitch (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    engaged         BOOLEAN     NOT NULL DEFAULT FALSE,
    reason          TEXT,
    engaged_at      TIMESTAMPTZ
);
INSERT INTO ftr_killswitch (id, engaged) VALUES (1, FALSE)
    ON CONFLICT (id) DO NOTHING;
