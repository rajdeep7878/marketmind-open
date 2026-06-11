-- Trader v1: strategy identity + immutable version snapshots.
--
-- `trader_strategies` is the logical strategy identity (a stable name +
-- description). Versions of that strategy live in
-- `trader_strategy_versions`, one row per snapshotted StrategySpec
-- taken from MarketMind's `extracted_strategies` at operator approval
-- time. Trader is decoupled from MarketMind's evolving schema: once a
-- version is snapshotted, the spec / parameters / backtest metrics are
-- frozen — paper-trade history is anchored to a fixed input.
--
-- "Append-only" is enforced via a trigger that rejects mutation of any
-- column other than operator-controlled status flags (`enabled`,
-- `approved_for_paper`, `notes`). The admin endpoints toggle those
-- three; everything else is locked at insert. `approved_for_live` is
-- frozen at FALSE in v1 — the trigger explicitly disallows changing
-- it, so even an accidental UPDATE can't move a strategy to live.

CREATE TABLE IF NOT EXISTS trader_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE IF NOT EXISTS trader_strategy_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id UUID NOT NULL REFERENCES trader_strategies (id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    -- The upstream extracted_strategies.id this version snapshotted
    -- from. Not a FK: extractions live in MarketMind's domain and may
    -- be pruned independently of the trader's history.
    marketmind_spec_id UUID NOT NULL,
    template TEXT NOT NULL CHECK (template IN (
        'ma_trend', 'breakout', 'rsi_mean_reversion', 'bb_mean_reversion', 'vcb'
    )),
    parameters JSONB NOT NULL,
    -- TEXT[] for the symbols/timeframes list rather than a child table
    -- because v1 typically has 1–3 symbols per version; the join cost
    -- of a relational form isn't worth it. Query with `= ANY(symbols)`.
    symbols TEXT[] NOT NULL,
    timeframes TEXT[] NOT NULL,
    risk_pct NUMERIC NOT NULL,
    fee_bps NUMERIC NOT NULL,
    slippage_bps NUMERIC NOT NULL,
    -- Snapshot of the upstream backtest result JSON. MUST include the
    -- walk-forward out-of-sample numbers — the drift analyzer compares
    -- live paper performance against backtest_metrics->>'walk_forward'
    -- and the approve_paper admin endpoint rejects approval if that
    -- subtree is missing.
    backtest_metrics JSONB NOT NULL,
    overfitting_metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    approved_for_paper BOOLEAN NOT NULL DEFAULT FALSE,
    -- Never flipped to TRUE in v1. The immutability trigger blocks
    -- updates to this column regardless of caller intent.
    approved_for_live BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy_id, version)
);


CREATE INDEX IF NOT EXISTS trader_strategy_versions_strategy_idx
    ON trader_strategy_versions (strategy_id, version DESC);

-- Hot-path index for the signal loop: only enabled + paper-approved
-- versions are eligible for evaluation. Partial keeps the index small.
CREATE INDEX IF NOT EXISTS trader_strategy_versions_approved_idx
    ON trader_strategy_versions (strategy_id)
    WHERE approved_for_paper = TRUE AND enabled = TRUE;


-- Append-only enforcement. The three mutable columns
-- (`enabled`, `approved_for_paper`, `notes`) are the operator's
-- control surface; everything else is the frozen snapshot. Trying to
-- mutate a frozen column — including `approved_for_live`, which v1
-- pins to FALSE — raises an exception that the admin endpoints
-- surface as a 422.
CREATE OR REPLACE FUNCTION trader_strategy_versions_immutability()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.strategy_id IS DISTINCT FROM OLD.strategy_id
        OR NEW.version IS DISTINCT FROM OLD.version
        OR NEW.marketmind_spec_id IS DISTINCT FROM OLD.marketmind_spec_id
        OR NEW.template IS DISTINCT FROM OLD.template
        OR NEW.parameters IS DISTINCT FROM OLD.parameters
        OR NEW.symbols IS DISTINCT FROM OLD.symbols
        OR NEW.timeframes IS DISTINCT FROM OLD.timeframes
        OR NEW.risk_pct IS DISTINCT FROM OLD.risk_pct
        OR NEW.fee_bps IS DISTINCT FROM OLD.fee_bps
        OR NEW.slippage_bps IS DISTINCT FROM OLD.slippage_bps
        OR NEW.backtest_metrics IS DISTINCT FROM OLD.backtest_metrics
        OR NEW.overfitting_metrics IS DISTINCT FROM OLD.overfitting_metrics
        OR NEW.approved_for_live IS DISTINCT FROM OLD.approved_for_live
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'trader_strategy_versions is append-only except for enabled / approved_for_paper / notes'
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trader_strategy_versions_immutability_trg ON trader_strategy_versions;
CREATE TRIGGER trader_strategy_versions_immutability_trg
    BEFORE UPDATE ON trader_strategy_versions
    FOR EACH ROW
    EXECUTE FUNCTION trader_strategy_versions_immutability();
