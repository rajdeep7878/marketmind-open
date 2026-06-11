# Hunt 19 — BTC/USDT 4H 10-day MAX Breakout with EMA(50) Trend Filter

**Phase:** post-C.7 fast-cadence hunt session
**Symbol:** BTC/USDT
**Timeframe:** 4H
**Asset class:** crypto_spot
**Source type:** raw_text (operator-written; grounded in Padyšák & Vojtko 2022 SSRN)
**Date:** 2026-05-27
**Expected primitives:** `Highest(close, 60)`, `EMA(50)`, `compare`, `ATR(14)`, `StopLossAtrMultiple`

## Why this hunt

Fast-cadence search post Hunt 17. Hunt 17 (200-EMA dual trend filter)
passed the gauntlet at likely_robust 16.19 but requires 1005 bars of
warmup (5x EMA-200 oversample) = ~127 days before first trade.

This hunt targets the **same confirmation-layered shape but FAR
shorter lookback**. Padyšák & Vojtko's 2022 SSRN paper found 10-day
MAX (highest close in last 10 days) on BTC 4H produced robust OOS
results — but the bare 10-day MAX signal would be a Hunt-18-style
single-signal failure. Adding an EMA(50) trend filter gives the
Hunt-17-style confirmation layer the gauntlet rewards. Max lookback
is 60 bars (10 days at 4H) for the Highest indicator, 50 bars × 5 =
250 bars for EMA(50) warmup = **42 days warmup** in production.

## Cost-sanity (pre-extraction)

- Implied trade frequency: ~12-15 round-trip/year at 4H BTC
  (10-day breakouts in confirmed uptrend are uncommon events)
- Per-side cost: 15 bps, round-trip 30 bps
- Annual cost drag: 15 × 30 bps = **4.5% / year**
- Author claim: Padyšák reports Sharpe ~1.1 on the bare MAX-only leg
  with added confirmation expected to preserve Sharpe + boost OOS
  consistency
- Cost-survivability: comfortable (under 5% drag, claimed edge >>15%)

## Source text

> Padyšák and Vojtko's 2022 SSRN paper "Seasonality, Trend-following,
> and Mean reversion in Bitcoin" tested a battery of simple
> trend-following signals on BTC at multiple lookbacks. The strongest
> robust finding was that a 10-day breakout (entering when the close
> reaches a new 10-day high) outperformed both shorter (5-day) and
> longer (20-day, 60-day) lookbacks on out-of-sample data. The
> mechanism: 10 days is short enough to react to genuine momentum
> regimes but long enough to filter out hourly noise. The paper's
> bare 10-day MAX-only leg retained its effectiveness on the 2022-2024
> out-of-sample extension while the MIN-only leg degraded.
>
> The bare 10-day MAX signal alone is too noisy for a paper-bot —
> it triggers on every 10-day-high event regardless of broader trend
> context, generating both real breakouts and dead-cat-bounce traps
> during downtrends. The professional adaptation adds a slower trend
> filter: only enter the 10-day MAX breakout if the medium-term trend
> is also bullish. The 50-period EMA on 4H bars (≈8 days) is the
> conventional medium-term filter for this purpose.
>
> The rules, adapted to BTC/USDT 4H bars:
>
> **Instrument:** BTC/USDT spot on Binance, the most liquid crypto pair.
>
> **Timeframe:** 4-hour bars. 10 days = 60 4H bars.
>
> **Direction:** long only.
>
> **Entry:** Go long when BOTH of the following hold on the close of a
> 4H bar:
> 1. The close price equals or exceeds the highest close of the
>    preceding 60 bars (10-day breakout).
> 2. The close price is above the 50-period EMA of close (medium-term
>    uptrend confirmation).
>
> Entry fires on bar close (signal); fill on the next-bar open at
> realised slippage.
>
> **Exit:** Two paths, whichever fires first:
> 1. The close price drops below the 50-period EMA of close
>    (trend filter flips bearish).
> 2. A hard stop-loss at 3 × ATR(14) below the entry price
>    (flash-crash protection — required for seeding in MarketMind's
>    paper bot per the "every entry needs a protective stop"
>    requirement).
>
> **Position sizing:** 1% of equity per trade, fixed percent.
>
> **Expected trade frequency:** approximately 12-15 round-trip trades
> per year on BTC/USDT 4H. The dual filter (10-day MAX + EMA(50)
> trend confirmation) makes signals rare — only firing on legitimate
> breakouts within established uptrends.
>
> **Author claim:** Padyšák & Vojtko's 2022 SSRN paper reports
> Sharpe ratios of 1.0-1.2 on the 10-day MAX leg over 2015-2021
> in-sample, with positive out-of-sample extension through 2024.
> Adding the EMA(50) trend filter is expected to preserve the
> Sharpe (or modestly improve it via reduced false-positive entries)
> while substantially boosting out-of-sample consistency. The
> baseline max drawdown of the bare MAX leg was approximately 30%;
> the filtered version is expected to be similar or smaller.
>
> The hypothesis: a confirmation-layered 10-day MAX breakout in
> confirmed BTC uptrends should clear the gauntlet's walk-forward +
> parameter-sweep + monte-carlo + deflated-Sharpe tests at the
> likely_robust threshold. The Hunt 17 finding (dual-EMA confirmation
> at composite 16.19 vs raw TSMOM at 46.32) provides direct
> precedent: confirmation layers are the structural feature that
> separates seedable from rejectable signals on BTC.

## Expected schema shape

- `instrument.symbol = "BTC/USDT"`, asset_class = "crypto_spot"
- `primary_timeframe = "4h"`, direction = "long"
- `entry.condition` = AND(`compare(close >= highest(close, 60))`, `compare(close > EMA(close, 50))`)
- `exit.exits` = [`compare(close < EMA(close, 50))` condition exit, `StopLossAtrMultiple(atr_period=14, mult=3.0)`]
- `position_sizing` = fixed_percent 1%
- `costs` = crypto defaults

## Attribution

Operator-written, grounded in Padyšák & Vojtko 2022 SSRN paper
("Seasonality, Trend-following, and Mean reversion in Bitcoin") + the
Hunt 17 confirmation-layer finding. EMA(50) trend-filter addition is
operator-applied to satisfy the seed protocol's confirmation-layer
requirement.
