# Hunt 24 — BTC/USDT 1H RSI Oversold Mean-Reversion (trend-gated)

**Phase:** v2 mean-reversion primitive diagnostic (parallel hunt cluster, lane B)
**Symbol:** BTC/USDT · **Timeframe:** 1h (primary), 4h (trend filter) · **Asset class:** crypto_spot
**Source type:** raw_text — operator synthesised, mean-reversion primitive test
**Date:** 2026-06-04 · **Gauntlet ref:** `87b930e` (clean tree, no shared-source edits)
**Primitives exercised:** `RSICondition` (0015), `RMultipleExit` (0018), cross-timeframe `compare` (4H EMA filter)

## Verdict: **REJECT** — `mixed_signals`, composite **43.25** (band 33.2–53.2)

`mixed_signals` is an automatic REJECT under the seed rule (only `likely_robust`
≈ below 33 inverted seeds). Not marginal-near-threshold; squarely mixed.

## Why this hunt — diagnostic role

The classic oscillator mean-reversion shape — buy RSI(14) < 30 — gated by a
higher-timeframe trend filter (4H close > EMA(50)) so we only buy oversold dips
*inside* a 4H uptrend. Long-only. Exit is a fixed 1:2 R-multiple ATR bracket
(`RMultipleExit` stop_R 1.0 / target_R 2.0, R = 1×ATR(14) at entry). Tests
whether oscillator mean-reversion carries edge at an *intermediate* (1H) cadence
once the ~30 bps round-trip cost is honestly charged — the cadence between the
trend-strong 4H boundary and the trend-dead 15m boundary.

## Cost-sanity (pre-extraction → confirmed empirically)

- Pre-flight projection: 1H RSI<30 gated by a 4H uptrend fires far less than 15m.
  Projected ~20–40 trades/yr → 30 × 0.30% ≈ 9% per-notional annual drag. Gentle
  vs 15m; PASS the >15% pre-extraction gate, proceed.
- **Empirical:** 112 trades / 3.92 yr = **28.6 trades/yr** → **8.6%/yr per-notional
  cost drag**. Projection held.
- **Cost-sanity ratio = return / drag = NEGATIVE** (return is −0.38% total) → **FAIL.**

## Extraction

- Model: Sonnet 4.6 (extraction service), `fully_extractable`, $0.044, clean —
  no fix needed. RSICondition + cross-TF EMA compare + RMultipleExit all produced
  first-try. Only post-process: set top-level `filter_timeframe="4h"` (the
  extractor declared 4h per-condition but left the loader-facing top-level field
  null; required for the engine to fetch 4H data — see meta report).

## Backtest (window 2022-06-01 → 2026-05-01, ~3.9y, 34,319 1H bars)

| metric | value |
|---|---|
| trades | 112 |
| total return | **−0.38%** |
| CAGR | −0.10% |
| Sharpe | **−1.07** |
| Sortino | −1.46 |
| win rate | 33.0% |
| profit factor | 0.63 |
| max drawdown | 0.40% |
| expectancy | −0.34% / trade |

(returns at engine-default sizing; the cost-sanity *ratio* and all gauntlet
risk-adjusted metrics are position-size invariant.)

### Drift parity — cross-engine envelope (correct shape for stateless additive primitives)
- vbt: 112 trades / −0.38% · iterative: 151 trades / −0.60% · **ratio 1.35× (within ±2×) → PASS.**
- Both engines agree: unprofitable. RMultipleExit decomposition (`decompose_r_multiple`
  → StopLossAtrMultiple + TakeProfitAtrMultiple) verified identical across both.

### Empirical trade inspection (run-before-assert)
First trade: entry 2022-07-10 14:00 @ 20874.4 → exit 2022-07-11 01:00 @ 20555.2
(−1.53%, a 1R stop hit). Loss cluster ≈ 1×ATR%, win cluster ≈ 2×ATR% — the 1:2
bracket geometry is confirmed applied. NB: the vbt path labels every R-multiple
exit `exit_reason="signal"` (generic attribution; the stop/target distances prove
the bracket fired — cosmetic, not a correctness issue).

## Rejection diagnostic (the real deliverable)

| bucket | metric | value | reading | pts × weight |
|---|---|---|---|---|
| **Walk-forward** | degradation_ratio | 0.0 (invalid: IS & OOS both negative) | **0/6 OOS-positive windows**; IS_avg −0.04%, OOS_avg −0.04% | 75 × 0.35 = 26.3 |
| **Param sweep** | peakiness | 0.0 | flat plateau, baseline at 60th pctile of grid — **NOT overfit to parameters** | 0 × 0.25 = 0.0 |
| **Monte Carlo** | p_value | **0.02** | real beats 98% of return-shuffled permutations — **a faint entry-timing signal exists** | 8 × 0.25 = 2.0 |
| **Deflated Sharpe** | prob_real_v2 | **3.3e-07** | observed Sharpe −1.07 → ~0 probability the edge is real | 100 × 0.15 = 15.0 |
| | | | **composite** | **43.25** |

**Primary rejection driver:** there is no *net* edge. DSR binds hardest
(negative Sharpe ⇒ prob_real ≈ 0) and walk-forward confirms (0/6 OOS windows
positive). The strategy is **both faintly-real-and-cost-eaten**: the MC p-value
(0.02) says RSI-oversold-in-uptrend does pick slightly-better-than-random
entries, but a 33% win rate at 1:2 R:R is right at the breakeven line
(0.33×2 ≈ 0.66 vs 0.67 loss-side) and the 8.6%/yr cost drag tips it negative.
The sweep clears (peakiness 0) — this is **not** an overfit; it's a genuinely
thin signal that costs erase. That distinction is the finding.

## Attribution
Operator-synthesised diagnostic candidate, lane B of the 2026-06-04 parallel
mean-reversion hunt cluster. First production exercise of `RSICondition` +
`RMultipleExit` on a cross-timeframe-gated long-only mean-reversion shape.
