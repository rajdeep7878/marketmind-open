# Hunt 20 — BTC/USDT 4H Triple-EMA Cascade (10/30/60)

**Phase:** post-C.7 fast-cadence hunt session
**Symbol:** BTC/USDT
**Timeframe:** 4H
**Asset class:** crypto_spot
**Source type:** raw_text (operator-written; grounded in Grayscale 2023 "Trend is Your Friend" + Carver canonical TSMOM)
**Date:** 2026-05-27
**Expected primitives:** `EMA(10)`, `EMA(30)`, `EMA(60)`, `compare`, `ATR(14)`, `StopLossAtrMultiple`

## Why this hunt

Hunt 19 (10-day MAX + EMA(50) filter) seeded successfully at composite
16.94 with ~11 days warmup. Hunt 20 tests a DIFFERENT confirmation
mechanism: triple-EMA cascade where the entry condition is fast > medium > slow
EMA alignment. This is the canonical Grayscale/Carver TSMOM-with-regime-filter
shape with three layers of confirmation (vs Hunt 17's two-layer dual-EMA
and Hunt 19's MAX+single-EMA-filter).

If Hunt 20 also passes the gauntlet, it confirms that THE confirmation
layer matters more than its specific shape — multiple-EMA-cascade,
single-EMA-filter-on-MAX, dual-EMA-cross all pass when properly
layered. This is the meta-finding from the post-Hunt-17/18 contrast.

Lookback periods chosen to keep warmup fast:
- EMA(10) ≈ 1.7 days at 4H
- EMA(30) ≈ 5 days at 4H
- EMA(60) ≈ 10 days at 4H
- Max indicator period 60 × 5 = 300 bars warmup = **50 days**

## Cost-sanity (pre-extraction)

- Implied trade frequency: ~15-25 round-trip/year at 4H BTC
  (cascade-alignment signals fire more often than 10-day-MAX
  breakouts but less than single-MA crossovers)
- Per-side cost: 15 bps, round-trip 30 bps
- Annual cost drag: 20 × 30 bps = **6% / year** (per notional)
- At 1% position sizing: 0.06% per-equity drag
- Cost-survivability: very comfortable

## Source text

> Grayscale Research's 2023 report "The Trend is Your Friend: Managing
> Bitcoin's Volatility with Momentum Signals" examined trend-following
> across cryptocurrency markets and found that short-term moving averages
> in the 10-30 day range consistently produced the highest Sharpe
> ratios on BTC, when paired with a slower regime filter. The Grayscale
> finding is consistent with the broader AQR/Carver time-series-momentum
> literature, which holds that confirmation-layered trend signals
> outperform single-signal trend signals on out-of-sample data.
>
> The triple-EMA cascade is the canonical Carver "Systematic Trading"
> chapter-3 implementation of this principle. The strategy fires a
> long entry only when three EMAs of increasing length are aligned in
> rising order (fast > medium > slow), and exits when any of the
> alignments break. This produces a cleaner signal than a single MA
> crossover because it requires THREE timeframes of price action
> (very short term, medium term, longer term) to all agree on the
> trend direction.
>
> The rules, adapted to BTC/USDT 4H bars with fast lookbacks:
>
> **Instrument:** BTC/USDT spot on Binance.
>
> **Timeframe:** 4-hour bars. EMAs use 10-bar, 30-bar, and 60-bar
> lookbacks corresponding roughly to 1.7 days, 5 days, and 10 days
> of price history respectively.
>
> **Direction:** long only.
>
> **Entry:** Go long when ALL THREE of the following hold on the
> close of a 4H bar:
> 1. The 10-period EMA of close is above the 30-period EMA of close.
> 2. The 30-period EMA of close is above the 60-period EMA of close.
> 3. The current close price is above the 10-period EMA of close.
>
> The triple confirmation ensures we only enter when very short-term,
> medium-term, and longer-term trends all agree on the bullish
> direction. Entry fires on bar close (signal); fill on the next-bar
> open at realised slippage.
>
> **Exit:** Two paths, whichever fires first:
> 1. The 10-period EMA of close drops below the 30-period EMA of
>    close (fast-medium cross down).
> 2. A hard stop-loss at 3 × ATR(14) below the entry price.
>
> **Position sizing:** 1% of equity per trade, fixed percent.
>
> **Expected trade frequency:** approximately 15-25 round-trip
> trades per year on BTC/USDT 4H. The triple-alignment filter is
> more restrictive than single-MA crossovers but less restrictive
> than the dual-EMA-200 filter of slower variants.
>
> **Author claim:** Grayscale's 2023 report and the AQR/Carver TSMOM
> literature both report Sharpe ratios of 0.9-1.2 for fast-cadence
> cascade signals on BTC over 2018-2024 (in-sample). The triple-EMA
> alignment is expected to preserve this Sharpe while improving
> out-of-sample consistency vs single-signal variants. Max drawdown
> is expected to be similar to the dual-EMA-200 strategy (~5-10% at
> 1% sizing).

## Expected schema shape

- `instrument.symbol = "BTC/USDT"`, asset_class = "crypto_spot"
- `primary_timeframe = "4h"`, direction = "long"
- `entry.condition` = AND(
    `compare(EMA(close, 10) > EMA(close, 30))`,
    `compare(EMA(close, 30) > EMA(close, 60))`,
    `compare(close > EMA(close, 10))`
  )
- `exit.exits` = [
    `compare(EMA(close, 10) < EMA(close, 30))` condition exit,
    `StopLossAtrMultiple(atr_period=14, mult=3.0)`
  ]
- `position_sizing` = fixed_percent 1%

## Attribution

Operator-written, grounded in Grayscale 2023 "Trend is Your Friend"
report + Carver "Systematic Trading" chapter 3 (triple-EMA cascade
canonical shape). Specific EMA periods (10/30/60) selected to keep
warmup under 60 days at 4H per the fast-cadence session brief.
