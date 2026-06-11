-- Trader v1: signal -> order -> fill -> position pipeline.
--
-- Pipeline: signal_engine writes a non-HOLD `trader_signals` row →
-- risk manager checks → executor writes a `trader_paper_orders` row
-- (status=PENDING, intended_fill_ts = open of candle N+1) → when that
-- candle is ingested, executor writes a `trader_paper_fills` row and
-- flips the order to FILLED → position is created (entry) or closed
-- (exit). HOLD signals are deliberately NOT persisted — they'd
-- dominate the table at zero informational value (the audit log
-- captures the "we looked and decided to hold" event instead).
--
-- Uniqueness on `trader_signals (strategy_version_id, symbol,
-- timeframe, candle_close_ts)` is the dedupe key for the signal loop:
-- a restart mid-cycle and a fresh evaluation of the same closed
-- candle must converge on the same row. ON CONFLICT DO NOTHING on
-- inserts makes the loop idempotent.
--
-- The partial UNIQUE INDEX on `trader_paper_positions` is the
-- on-database enforcement of "one open position per (strategy_version,
-- symbol)". Only rows with status='OPEN' are indexed, so closed
-- positions don't collide with future opens. Without this, a race
-- between two signal-execution cycles could double-open.

CREATE TABLE IF NOT EXISTS trader_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    candle_close_ts TIMESTAMPTZ NOT NULL,
    signal TEXT NOT NULL CHECK (signal IN ('BUY', 'SELL', 'EXIT', 'HOLD')),
    reason TEXT NOT NULL,
    indicators JSONB NOT NULL,
    proposed_entry_price NUMERIC NOT NULL,
    proposed_stop_price NUMERIC NOT NULL,
    proposed_take_profit_price NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ,
    UNIQUE (strategy_version_id, symbol, timeframe, candle_close_ts)
);

CREATE INDEX IF NOT EXISTS trader_signals_recent_idx
    ON trader_signals (created_at DESC);

-- Executor's "what do I need to do next?" query: orders that are
-- still PENDING and whose intended_fill_ts is at or before the
-- latest ingested candle. Sorted by intended_fill_ts so we fill in
-- chronological order across symbols.
CREATE INDEX IF NOT EXISTS trader_signals_unprocessed_idx
    ON trader_signals (processed_at, candle_close_ts)
    WHERE processed_at IS NULL;


CREATE TABLE IF NOT EXISTS trader_paper_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- UNIQUE: one order per signal. EXIT signals close an existing
    -- position via their own order row; a re-entry on the next cycle
    -- gets its own signal_id and so its own order row.
    signal_id UUID NOT NULL UNIQUE
        REFERENCES trader_signals (id) ON DELETE CASCADE,
    strategy_version_id UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    -- v1 is market-only. The CHECK keeps the column open to future
    -- order types but pins behaviour now.
    order_type TEXT NOT NULL CHECK (order_type IN ('MARKET')),
    requested_size NUMERIC NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'FILLED', 'REJECTED')),
    rejection_reason TEXT,
    -- The open timestamp of candle N+1 (signal fired on close of N).
    -- Executor polls for orders whose intended_fill_ts is now covered
    -- by an ingested closed candle.
    intended_fill_ts TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS trader_paper_orders_pending_idx
    ON trader_paper_orders (intended_fill_ts)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS trader_paper_orders_strategy_idx
    ON trader_paper_orders (strategy_version_id, requested_at DESC);


CREATE TABLE IF NOT EXISTS trader_paper_fills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- UNIQUE: no partial fills in v1. One order -> one fill, ever.
    order_id UUID NOT NULL UNIQUE
        REFERENCES trader_paper_orders (id) ON DELETE CASCADE,
    fill_ts TIMESTAMPTZ NOT NULL,
    fill_price NUMERIC NOT NULL,
    size NUMERIC NOT NULL,
    fee NUMERIC NOT NULL,
    slippage_bps_applied NUMERIC NOT NULL,
    notional NUMERIC NOT NULL
);

CREATE INDEX IF NOT EXISTS trader_paper_fills_recent_idx
    ON trader_paper_fills (fill_ts DESC);


CREATE TABLE IF NOT EXISTS trader_paper_positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    -- v1: long-only spot. CHECK keeps the column open for SHORT later.
    side TEXT NOT NULL CHECK (side IN ('LONG')),
    -- RESTRICT (not CASCADE) so deleting an order can't silently
    -- wipe out a position record; the operator has to choose.
    entry_order_id UUID NOT NULL
        REFERENCES trader_paper_orders (id) ON DELETE RESTRICT,
    exit_order_id UUID
        REFERENCES trader_paper_orders (id) ON DELETE RESTRICT,
    entry_price NUMERIC NOT NULL,
    entry_ts TIMESTAMPTZ NOT NULL,
    exit_price NUMERIC,
    exit_ts TIMESTAMPTZ,
    size NUMERIC NOT NULL,
    -- A stop price is mandatory. The trader's invariant: no stop = no
    -- trade. The strategy template / risk manager guarantees this
    -- before any open ever reaches the INSERT.
    stop_price NUMERIC NOT NULL,
    take_profit_price NUMERIC,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    realised_pnl NUMERIC,
    realised_pnl_pct NUMERIC,
    -- Close reason values are open-ended (the audit trail benefits
    -- from free-form context), but the executor only writes one of
    -- 'signal_exit', 'stop_hit', 'take_profit_hit', 'manual'.
    close_reason TEXT
);

CREATE INDEX IF NOT EXISTS trader_paper_positions_strategy_idx
    ON trader_paper_positions (strategy_version_id, entry_ts DESC);

-- The one-open-position invariant: partial unique index restricts
-- conflict detection to rows where status='OPEN'. Closed positions
-- can stack up freely for the same (strategy_version, symbol) tuple
-- across time.
CREATE UNIQUE INDEX IF NOT EXISTS trader_paper_positions_one_open_idx
    ON trader_paper_positions (strategy_version_id, symbol)
    WHERE status = 'OPEN';
