"""Backtest engine: indicators, spec->signals translator, vectorbt runner.

The three concerns are split into separate modules:

  - indicators: pure-function indicator computation. One function per
    Phase 1 whitelist entry, plus the candle-pattern helpers. Inputs
    are OHLCV DataFrames + params; outputs are Series (or DataFrames
    for multi-output indicators like MACD / Bollinger / Stochastic).
  - translator: turn a validated StrategySpec into the boolean entry
    and exit signal arrays vectorbt needs. Walks the Condition tree,
    handles multi-timeframe alignment under no-look-ahead, raises
    TranslationError on un-executable shapes.
  - engine: wire it all together. Pull market data, build signals,
    call vectorbt.Portfolio.from_signals with the spec's cost model
    and sizing, return a BacktestRun.

Phase 3.1 stops at the BacktestRun. Metrics + author comparison + UI
land in 3.2.
"""
