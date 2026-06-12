# Changelog

All notable changes to MarketMind (public repo). Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

The full pre-release engineering history of the private research project
this repo was curated from lives in [docs/project_log.md](docs/project_log.md)
and the research record in [docs/hunts/](docs/hunts/) and
[docs/ftr/REPORT.md](docs/ftr/REPORT.md).

## [1.0.0] — 2026-06-12

### Added — initial public release

- Strategy-spec schema (pydantic discriminated unions, validator,
  multi-leg/perp extensions).
- Dual backtest engines (vectorized + iterative event-driven) with
  CI-gated drift parity, plus perp pairs / trend / carry engines.
- Overfitting gauntlet: walk-forward, parameter sweep, Monte Carlo,
  Deflated Sharpe (honest n_trials, T in years), composite scoring.
- Per-venue fee/slippage cost models with sensitivity stress.
- FTR research stack: QA-gated data layer, keyless L1/L2 recorder,
  anti-lookahead feature pipelines, four research strategies, G1–G9
  verdict gates, Decimal-ledger paper trader.
- Paper-only safety walls in both traders, each backed by tests.
- Full honest research record: 20 hunt write-ups + the FTR validation
  report (every strategy rejected at realistic retail costs).
- Two-container Docker setup + opt-in FTR profile; ~450
  fixture-independent tests.

### Not included (by design)

- Market-data fixtures (exchange ToS — regenerate via the keyless fetch
  scripts documented in the README).
- Any live-trading execution path.
