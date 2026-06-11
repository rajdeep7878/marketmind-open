-- Trader v2 (A.5b): per-strategy stateful-condition persistence.
--
-- A.5b persists the Tier-2 evaluation state (regime latches, ratchet
-- extrema) of stateful `spec`-template versions so the live latch is
-- full-history-exact rather than re-derived from each cycle's truncated
-- candle window (design doc §6A.1, §6B). v1 templates and non-stateful
-- specs never touch this table — they write zero rows.
--
-- Append-only: a state advance INSERTs a new row; rows are never
-- UPDATEd. The full trajectory is its own audit log. The current state
-- for a (version, symbol, timeframe) is `ORDER BY candle_close_ts DESC
-- LIMIT 1` over the index below — there is no maintained "is current"
-- flag (that would need an UPDATE per advance, defeating append-only).
--
-- The UNIQUE (version, symbol, timeframe, candle_close_ts) mirrors
-- trader_signals (migration 0008): it is the cross-worker idempotency
-- net — INSERT ... ON CONFLICT DO NOTHING drops a duplicate advance of
-- the same candle (design doc §6A.2).
--
-- Purely additive: a new table, no change to any existing table or row.

CREATE TABLE IF NOT EXISTS trader_strategy_state (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id  UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    symbol               TEXT        NOT NULL,
    timeframe            TEXT        NOT NULL,
    -- The closed candle this state row is "as of". The seed for the next
    -- cycle's evaluation; also the idempotency-guard key (§6A.2).
    candle_close_ts      TIMESTAMPTZ NOT NULL,
    -- The StrategyState payload — regime latches + ratchet extrema.
    state                JSONB       NOT NULL,
    -- Which StrategyState shape wrote the row; lets a future schema
    -- migrate forward without a table change (design doc §6A.1).
    state_schema_version INTEGER     NOT NULL DEFAULT 1,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy_version_id, symbol, timeframe, candle_close_ts)
);

-- Current-state lookup: the DESC index makes "most recent row for this
-- (version, symbol, timeframe)" an index-only scan.
CREATE INDEX IF NOT EXISTS ix_trader_strategy_state_current
    ON trader_strategy_state (strategy_version_id, symbol, timeframe, candle_close_ts DESC);
