"""MarketMind Trader v1 — paper-only crypto execution engine.

The trader runs as a separate worker process on the `trader_default`
RQ queue. It reads candles via ccxt (no private endpoints), evaluates
five deterministic strategy templates over closed candles, applies a
risk-management gate, simulates paper fills at the next candle's open,
and tracks an equity curve + drawdown + drift versus the backtest
that approved each strategy.

Invariants:
- Paper trading only. No code path places a real order. The single
  guard is `trader.config.assert_paper_only()`, called as the literal
  first line of every job callable in `trader.jobs`.
- No LLM in the decision path. The bot must run cleanly with
  `ANTHROPIC_API_KEY` unset.
- Determinism. Strategy templates read only the candle history they
  are passed and never call `datetime.now()` or any other source of
  wall-clock or randomness.
- Decimal money. Every monetary value is `decimal.Decimal`. ccxt
  floats are converted at the ingestion boundary; vectorbt-style
  float pipelines never touch trader money math.
- UTC timestamps. Every datetime is tz-aware UTC.

Reuses the existing indicator math from
`marketmind_workers.backtest.indicators` so signal computation is
byte-identical to the backtester by construction (same module
instance, not a parallel implementation).
"""
