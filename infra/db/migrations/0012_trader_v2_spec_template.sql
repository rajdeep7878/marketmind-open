-- Trader v2 (A.5a): the generic SpecTemplate.
--
-- A.5a adds TemplateName.SPEC — a generic template that carries a v2
-- StrategySpec in `trader_strategy_versions.parameters` and evaluates it
-- through the shared backtest condition evaluators (one evaluator, one
-- source of truth — design doc §6A.0). This migration widens the
-- `template` CHECK constraint to admit the new kind.
--
-- The original constraint (migration 0006) was an inline column CHECK,
-- which PostgreSQL auto-names `trader_strategy_versions_template_check`.
-- We DROP IF EXISTS that name and re-ADD it with `spec` included. The
-- DROP+ADD form is what `test_trader_enum_db_parity` resolves as the
-- effective CHECK set, and is idempotent enough for the file-based
-- migration runner (each file is applied exactly once).
--
-- Purely additive: existing rows (the five v1 templates) still satisfy
-- the widened constraint, so this is safe to apply on a populated DB.

ALTER TABLE trader_strategy_versions
    DROP CONSTRAINT IF EXISTS trader_strategy_versions_template_check;

ALTER TABLE trader_strategy_versions
    ADD CONSTRAINT trader_strategy_versions_template_check
    CHECK (template IN (
        'ma_trend', 'breakout', 'rsi_mean_reversion', 'bb_mean_reversion', 'vcb', 'spec'
    ));
