# Changelog

All notable changes to MarketMind AI. Format adapted from
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), with
`v1.x` versions tagged on merge of completed sub-phase branches.

## [v1.2.0-final] — 2026-05-25

### Added — 5 schema primitives

Each surfaced by real-strategy extractions in the post-Phase-B
9-hunt era and closes a documented `partially_extractable` /
`not_extractable` outcome from that era.

- **`PercentileExpr`** (v1.2.A) — new `Expression` variant. Rolling
  empirical percentile (0..1) of an inner expression over a
  trailing `window` (bounds 10..10_000). Surfaced by Hunt 3
  (Momentum + ATR-percentile).
- **`prior_trade(predicate="bars_since_last_at_least")`** (v1.2.B) —
  5th literal in `PriorTradeCondition.predicate` enum; time-based
  re-entry throttle distinct from the four outcome-based predicates.
  `n` upper bound widened 100 → 100_000 to accommodate bars-since
  use cases. Surfaced by Hunt 5 (Mean-rev + Tier-3 throttle).
- **`TimeOfDayCondition`** (v1.2.C) — 13th `Condition` variant.
  Hour-of-day UTC gate with wrap-around windows (start > end spans
  midnight) and inclusive/exclusive end. Surfaced by Hunt 6B
  (Intraday seasonality).
- **`DayOfWeekCondition`** (v1.2.D) — 14th `Condition` variant.
  UTC weekday gate (pandas convention Mon=0..Sun=6) with at least
  one weekday, no duplicates.
- **`TakeProfitAtrMultiple`** (v1.2.E) — 4th `TakeProfitMethod`
  variant. `mult × ATR(atr_period)` above entry. Symmetric to
  `StopLossAtrMultiple`; vbt path applies the sign for SHORT via
  `direction="shortonly"`.

### Added — operator-facing docs

- `docs/design/v1.2-schema-additions.md` — design pass (locked
  before implementation).
- `docs/operations/v1.2-baseline-regression.md` — v1.2.0 baseline.
- `docs/operations/v1.2-final-regression.md` — final regression
  sweep (v1.2.F sign-off).
- `docs/v1.2_retrospective.md` — retrospective + the META-PATTERN
  ("test docstrings against theoretical mental model; empirics
  differ") codified as a standing rule for v1.3+.

### Changed

- **Extraction prompt** (`workers/.../services/extraction_prompt.py`)
  gained five new sections under `## SCHEMA REFERENCES` (one per
  new primitive) plus a restructured `### prior_trade` section
  documenting the new time-based predicate alongside the four
  outcome-based predicates. Prompt-cache is invalidated on next
  extraction call (unavoidable for any prompt-shape change;
  documented in design doc §6 risk register).
- **`TradeHistory.evaluate_predicate`** signature widened with
  keyword-only `current_bar: int | None = None` for the new
  `bars_since_last_at_least` predicate. Existing call sites
  byte-identical (the four outcome-based predicates ignore
  `current_bar`).
- **`_compute_take_profit`** in `spec_template.py` gained a
  `candles: pd.DataFrame | None = None` parameter for the
  ATR-multiple branch (with defensive `ValueError` if candles is
  None when an AtrMultiple TP is supplied).
- **`_atr_for_stop`** in `iterative.py` extended to also detect
  `TakeProfitAtrMultiple` via a new `tp_method` parameter.
- **Overfitting parameter-sweep** axis-detection
  (`_detect_axes`) recognises `PercentileExpr.window` via the new
  `SweepAxisKind.PERCENTILE_WINDOW` enum entry.

### Unchanged — v1 / v2 / Phase A / Phase B contracts preserved

- **227 pre-v1.2 spec corpus tests** pass bit-identical.
- **3 production paper-trading strategies** (BB Breakout EMA200,
  Golden Cross 50/200 SMA, Modern Turtle Donchian) run with
  bit-identical behavior — no spec touches a v1.2 primitive, so
  no evaluator path changed for them.
- **Phase A state primitives** (regime_state, ratchet,
  prior_signal, prior_trade existing predicates) unchanged.
- **Phase B FeeModel + SlippageModel + sqrt(N) drift scaling**
  unchanged.
- **`assert_paper_only()` PAPER-ONLY assertion** untouched.
- **trader_worker container** NOT restarted during the entire
  v1.2 cycle (30+ hour continuous uptime). The running container
  serves pre-v1.2 code; it's bit-identical to the new code for
  the 3 seeded strategies. Opportunistic post-merge rebuild
  recommended but not urgent.

### Quality gates (final sweep)

- **Test count**: 1099 → **1199** (+100).
- **Drift-parity gates**: 5 → **76** cases (+71), zero divergence.
- **Perf-regression**: 1H + 15m Turtle backtests both PASSED well
  under threshold.
- **Ruff + pyright**: maintained 0 errors / 0 warnings continuously
  across all 21 v1.2 commits.
- **Suite wall-clock**: 83.84 s → **95.84 s** (+14 %, within the
  20 % gate).

### Total work

- **21 commits** on the `v1.2-schema-additions` branch (1 baseline
  + 5 sub-phases A-E + 3 sign-off = v1.2.0 + v1.2.A.1-5 + v1.2.B.1-4
  + v1.2.C.1-3 + v1.2.D.1-2 + v1.2.E.1-3 + v1.2.F.1-3).
- **6 working sessions** across 2 calendar days (2026-05-24 →
  2026-05-25).
- Within the design-doc estimate (4-6 sessions, ~18 commits).

---

## Pre-v1.2 history

Phase B (lower timeframes) complete at `phase-b-complete.md`,
2026-05-23. Phase A (stateful conditions) complete at
`phase-a-complete.md`, 2026-05-21. See those docs for prior phase
detail.
