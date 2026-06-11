# Hunt 21 — BTC/USDT 1H 5-Day MAX Breakout with EMA(100) Trend Filter

**Phase:** post-Hunt-20 fast-cadence diagnostic session
**Symbol:** BTC/USDT
**Timeframe:** 1H
**Asset class:** crypto_spot
**Source type:** raw_text (operator-written; Hunt-19 mechanism ported to 1H)
**Date:** 2026-05-27
**Expected primitives:** `Highest(close, 120)`, `EMA(100)`, `compare`, `ATR(14)`, `StopLossAtrMultiple`

## Why this hunt — diagnostic role

Hunts 17/19/20 established the confirmation-layered BTC 4H trend
pattern (composite 16-20, all `likely_robust`). Hunt 21 tests whether
the SAME mechanism survives at 1H cadence — 4x more bars, 4x more
signals, and significantly higher cost drag.

Hypothesis: the confirmation-layer structural property holds across
cadence, but COST may be the binding constraint at 1H. The diagnostic
will tell us which bucket (cost-sanity / walk-forward / MC / DSR)
drives the rejection if rejected, or confirm that confirmation-layer
shapes seed at 1H too.

Lookback choices:
- `Highest(120)` = 120h = 5 days (1H-cadence equivalent of Hunt 19's
  10-day MAX at 4H)
- `EMA(100)` = ~4-day trend filter (1H-cadence equivalent of Hunt 19's
  EMA(50) at 4H)
- Warmup: 5 × 100 = 500 bars × 1h = **~21 days**

## Cost-sanity (pre-extraction)

- Expected trade frequency: ~50-100 round-trip/year at 1H BTC
  (5-day MAX breakouts in confirmed uptrend, 4x more signals than 4H)
- Per-trade round-trip cost: 30 bps (10 commission + 5 slippage, both sides)
- Annual drag estimate: 75 trades × 30 bps = **~22% annual drag**
- Cost-survivability VERDICT: BORDERLINE — would need >5% annual measured edge
- Decision: extract anyway — DIAGNOSTIC value is exactly this borderline case

## Source text

> The confirmation-layered breakout pattern that proved seedable on BTC
> 4H bars (Hunt 17 dual-EMA, Hunt 19 MAX+EMA filter, Hunt 20 triple-EMA
> cascade) can be naturally ported to 1H bars by scaling the lookback
> periods 4x. The 1H variant captures faster-cadence trend signals and
> fires more often, at the cost of higher transaction-cost drag and
> potentially noisier signals.
>
> The specific port of Hunt 19's 10-day MAX + EMA(50) filter to 1H bars
> uses a 5-day MAX breakout (Highest of close over 120 1H bars) gated
> by a 4-day trend filter (close > 100-period EMA). The same hard-stop
> protection (3 × ATR(14)) and EMA-crossover exit complete the
> structure.
>
> The rules:
>
> **Instrument:** BTC/USDT spot on Binance.
> **Timeframe:** 1-hour bars.
> **Direction:** long only.
>
> **Entry:** Go long when BOTH of the following hold on the close of a
> 1H bar:
> 1. The close price equals or exceeds the highest close of the
>    preceding 120 bars (5-day breakout).
> 2. The close price is above the 100-period EMA of close (4-day
>    trend confirmation).
>
> **Exit:** Two paths, whichever fires first:
> 1. The close price drops below the 100-period EMA of close.
> 2. A hard stop-loss at 3 × ATR(14) below the entry price.
>
> **Position sizing:** 1% of equity per trade, fixed percent.
>
> **Expected trade frequency:** approximately 50-100 round-trip trades
> per year on BTC/USDT 1H. The dual filter (5-day MAX + EMA(100) trend
> confirmation) reduces signal noise but still fires roughly 4x more
> than the 4H equivalent due to the higher bar cadence.
>
> **Author claim:** the 4H equivalent (Hunt 19, Padyšák 10-day MAX +
> EMA(50)) produced Sharpe 1.08, Sortino 1.59, max drawdown 0.51% on
> 2020-2026 BTC 4H data. The 1H variant is expected to show similar
> Sharpe IF the underlying signal is genuine, but higher transaction
> cost drag may eat into the net return. The honest hypothesis: this
> is a borderline-cost-survivable strategy, and the gauntlet will
> tell us whether the edge survives realistic 1H-cadence costs.

## Expected schema shape

- `instrument.symbol = "BTC/USDT"`, asset_class = "crypto_spot"
- `primary_timeframe = "1h"`, direction = "long"
- `entry.condition` = AND(
    `compare(close >= lagged(highest(close, 120), 1))`,
    `compare(close > EMA(close, 100))`
  )
- `exit.exits` = [
    `compare(close < EMA(close, 100))` condition exit,
    `StopLossAtrMultiple(atr_period=14, mult=3.0)`
  ]

## Attribution

Operator-written port of Hunt 19's confirmation-layered mechanism to
1H cadence. Primarily a diagnostic candidate — designed to surface
whether confirmation-layer seedability holds at 1H, or whether cost-
eating becomes the binding constraint at higher cadence.
