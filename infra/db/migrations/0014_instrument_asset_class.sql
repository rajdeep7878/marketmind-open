-- Phase C C.1.5 (2026-05-26): denormalised asset_class column on
-- trader_strategy_versions.
--
-- Background. C.1.1 added Instrument.asset_class to the schema as a
-- Pydantic Literal (crypto_spot, fx_spot, metals_spot, equity_etf,
-- equity_single) with default "crypto_spot". The trader currently
-- reads asset_class from spec_json on every cycle — no DB column is
-- strictly required for the read path. This migration denormalises
-- the value into a column on `trader_strategy_versions` so:
--   * future filter / index queries (e.g. "show me all FX strategies
--     in paper") don't have to deserialise spec_json
--   * the per-cycle adapter dispatch (C.1.4) can route the trader by
--     strategy without re-deriving the class from spec_json
--   * Phase C ops dashboards (planned C.10/C.11) can aggregate by
--     asset class cheaply
--
-- Default `crypto_spot` backfills every existing row — all 3
-- production strategies + every test fixture are crypto_spot today.
-- A CHECK constraint pins the value set to the AssetClass Literal
-- members exactly. Typos at INSERT fail loudly, before the trader
-- factory's NotImplementedError branch is hit at dispatch time.
--
-- Backward compat. Pre-C.1.5 code paths that SELECT from
-- trader_strategy_versions without listing `asset_class` continue to
-- work unchanged (the column is denormalised, not load-bearing for
-- existing queries). The trader_worker container can keep running
-- through this migration without restart — the new column is invisible
-- to the running cycle's SQL until the trader_worker boots a worker
-- that uses it (none today; C.6+ will).
--
-- ADD COLUMN IF NOT EXISTS is the idempotent shape — re-running the
-- migration is a no-op rather than a hard error.

ALTER TABLE trader_strategy_versions
    ADD COLUMN IF NOT EXISTS asset_class TEXT NOT NULL DEFAULT 'crypto_spot'
        CHECK (asset_class IN (
            'crypto_spot',
            'fx_spot',
            'metals_spot',
            'equity_etf',
            'equity_single'
        ));

COMMENT ON COLUMN trader_strategy_versions.asset_class IS
    'Phase C C.1.5: denormalised cache of the spec''s Instrument.asset_class. '
    'CHECK constraint pins the value set to the AssetClass Literal members. '
    'Default crypto_spot backfills pre-C.1 rows. New strategy seeds (C.6+) '
    'should populate this from the spec at insert time.';
