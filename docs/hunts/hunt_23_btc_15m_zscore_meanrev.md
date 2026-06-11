# Hunt 23 — BTC/USDT 15m Z-Score Mean-Reversion (4H trend + volatility gate)

**Phase:** v2 mean-reversion primitive diagnostic (parallel hunt cluster, lane A)
**Symbol:** BTC/USDT · **Timeframe:** 15m (primary), 4h (trend filter) · **Asset class:** crypto_spot
**Source type:** raw_text — operator synthesised, mean-reversion primitive test
**Date:** 2026-06-04 · **Gauntlet ref:** `87b930e` (clean tree, no shared-source edits)
**Primitives exercised:** `ZScoreCondition` (0017), `RMultipleExit` (0018), `PercentileExpr`(ATR vol-band), cross-timeframe `compare` (4H EMA filter)

## Verdict: **REJECT** — `mixed_signals`, composite **57.81** (band 47.8–67.8)

`mixed_signals` is an automatic REJECT under the seed rule. Composite 57.81 sits
just under the `likely_overfit` line (60) — the *worse* of the two hunts.

## Why this hunt — diagnostic role

Statistical mean-reversion at the extreme fast cadence (15m). Three-filter
selectivity stack designed to survive the ~30 bps round-trip cost:
1. **Statistical oversold:** ZScore(close, 20) < −2.0 (`ZScoreCondition` below_neg).
2. **HTF trend context:** 4H close > EMA(50) (long-only inside a 4H uptrend).
3. **Volatility band:** ATR(14) percentile over a trailing 500-bar window ∈ [0.25, 0.85]
   (skip dead tape *and* chaotic tape).
Exit: 1:2 R-multiple ATR bracket (`RMultipleExit` stop_R 1.0 / target_R 2.0,
R = 1×ATR(14) at entry). Tests whether the confirmation-layer discipline that
makes trend-following seedable at 4H rescues *mean-reversion* at 15m.

## Cost-sanity (pre-extraction → confirmed catastrophic empirically)

- Pre-flight: 15m fires often; even a 3-filter stack was projected at ~100–300
  trades/yr → ≥30%/yr per-notional drag. **>15% gate → flagged HIGH.** The
  3-filter stack *was* the tightening (vs naive ZScore-only); proceeded to let
  the empirical trade count be the arbiter (the diagnostic is the whole point).
- **Empirical:** 1,052 trades / 3.92 yr = **268 trades/yr** (2,078 raw entry
  signals; rest collapsed by position-already-open) → **80%/yr per-notional cost
  drag.** Even three filters cannot slow a 15m ZScore trigger to a survivable rate.
- **Cost-sanity ratio = return / drag = NEGATIVE** (return −2.58%) → **FAIL (hard).**

## Extraction

- Model: Sonnet 4.6, first call produced a structurally complete spec but failed
  validation. **One authorized in-session fix** (the only one needed):
  the percentile-expression's inner `atr` **indicator** carried `atr_period`
  (the `RMultipleExit` field name) where the `atr` indicator wants `period`.
  Deterministic rename `atr_period→period` on the 2 percentile-ATR nodes; re-validated
  clean. This is a *new-primitive convention bleed* — the LLM mixed RMultipleExit's
  `atr_period` field into an indicator node — worth feeding back into the extraction
  prompt's percentile+ATR worked example. Also set top-level `filter_timeframe="4h"`
  (loader needs it; see meta report). Extraction cost ≈ $0.24 incl. the one retry.

## Backtest (window 2022-06-01 → 2026-05-01, ~3.9y, 137,275 15m bars)

| metric | value |
|---|---|
| trades | 1,052 |
| total return | **−2.58%** |
| CAGR | −0.66% |
| Sharpe | **−5.28** |
| win rate | 39.6% |
| profit factor | 0.46 |
| max drawdown | 2.61% |

### Drift parity — cross-engine envelope (correct shape for stateless additive primitives)
- vbt: 1,052 trades / −2.58% · iterative: 1,663 trades / −5.06% · **ratio 1.58× (within ±2×) → PASS.**

### Empirical trade inspection (run-before-assert)
First trade: entry 2022-06-24 15:30 @ 20882.0 → exit 2022-06-24 17:00 @ 21198.5
(**+1.31%, a 2R target hit** — confirms the target leg fires, complementing
Hunt 24's first-trade stop hit). 90-minute hold — fast in/out, characteristic of
the cadence. `exit_reason="signal"` is the vbt path's generic R-multiple label.

## Rejection diagnostic (the real deliverable)

| bucket | metric | value | reading | pts × weight |
|---|---|---|---|---|
| **Walk-forward** | degradation_ratio | 0.0 (invalid: IS & OOS both negative) | **0/6 OOS-positive windows**; IS_avg −0.30%, OOS_avg −0.11% | 75 × 0.35 = 26.3 |
| **Param sweep** | peakiness | 0.0 | flat (25-cell grid), baseline at 48th pctile — **NOT param-overfit; uniformly bad** | 0 × 0.25 = 0.0 |
| **Monte Carlo** | p_value | **0.46** | real ≈ random (54th pctile of permutations; synth_mean −2.61% ≈ real −2.58%) — **ZERO entry-timing edge** | 66 × 0.25 = 16.6 |
| **Deflated Sharpe** | prob_real_v2 | **2.3e-04** | observed Sharpe −5.28 → ~0 probability the edge is real | 100 × 0.15 = 15.0 |
| | | | **composite** | **57.81** |

**Primary rejection driver: Monte Carlo + DSR jointly — there is no signal at all.**
This is the sharpest result of the cluster: the 15m ZScore-oversold entry, even
trend- and volatility-gated, is **statistically indistinguishable from random
entry** (p=0.46). It is not a thin edge eaten by costs (that is Hunt 24's 1H story,
p=0.02); at 15m there is *no edge to eat* — and on top of that the 268-trades/yr
cost drag is the worst of any hunt to date. The flat sweep (peakiness 0) confirms
this is not an overfit artifact: the strategy is uniformly, structurally
unprofitable across its whole parameter neighborhood.

## Attribution
Operator-synthesised diagnostic candidate, lane A of the 2026-06-04 parallel
mean-reversion hunt cluster. First production exercise of `ZScoreCondition` +
`RMultipleExit` + a percentile-ATR volatility band on a cross-timeframe-gated
long-only mean-reversion shape.
