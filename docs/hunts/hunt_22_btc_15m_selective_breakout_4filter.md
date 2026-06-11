# Hunt 22 — BTC/USDT 15m Selective Multi-Filter Breakout

**Phase:** post-Hunt-20 fast-cadence diagnostic session
**Symbol:** BTC/USDT
**Timeframe:** 15m
**Asset class:** crypto_spot
**Source type:** raw_text (operator-written; 4-filter restrictive selective breakout)
**Date:** 2026-05-27
**Expected primitives:** `Highest(close, 48)`, `EMA(200)`, `TimeOfDayCondition`, `compare`, `ATR(14)`, `StopLossAtrMultiple`

## Why this hunt — diagnostic role

Pushing the cadence further down. Hunts 17/19/20 (4H) and 21 (1H)
all passed gauntlet with confirmation-layered shapes. Hunt 22 tests
the EXTREME cadence (15m) with an EXTREMELY restrictive multi-filter
stack designed to fire ~25-50 trades/year — selective enough that
the 30 bps round-trip cost remains survivable.

The 4-filter stack:
1. **Signal:** 12-hour MAX breakout (Highest(close, 48) at 15m)
2. **Trend confirmation:** close > EMA(close, 200) (~50 hours = ~2 days trend)
3. **Time-of-day filter:** UTC 8-22 (active liquidity hours; avoid Asian dead zone)
4. **Stop:** 3 × ATR(14) hard stop

If even this restrictive 15m strategy gets cost-eaten or rejected,
the diagnostic finding is: fast-cadence crypto-retail is genuinely
structurally hard at our 30 bps round-trip cost — no amount of
filter-stacking saves it.

If it PASSES, the finding is: selective multi-filter shapes scale
to extreme cadences (15m), and the seedability is structural to
the filter discipline, not to bar cadence.

Warmup: 5 × 200 = 1000 bars × 15m = **~10.4 days**

## Cost-sanity (pre-extraction)

- Expected trade frequency: ~50-100 round-trip/year on BTC 15m with
  restrictive 4-filter stack (12h MAX + 50h EMA + active hours)
- Per-trade round-trip cost: 30 bps
- Annual drag estimate: 75 × 30 bps = **22.5% annual drag (per notional)**
- At 1% sizing: 0.225% per equity per year drag
- For cost-sanity to pass: need >0.225% annual measured return
- Survivability: BORDERLINE — same range as Hunt 21

## Source text

> Fast-cadence (15-minute) crypto strategies are notorious for being
> cost-eaten. Generic breakout signals at 15m fire too often: a typical
> N-bar breakout on BTC 15m can trigger several times per day, and at
> 30 bps round-trip cost (10 bps commission + 5 bps slippage on each
> side, Binance spot), even a real per-trade edge of 50 bps gets
> mostly consumed.
>
> The professional adaptation is a multi-filter discipline: stack
> enough restrictive filters that the signal fires only when ALL
> conditions agree, reducing trade frequency from "several per day"
> to "1-2 per week". This is the same logic as Hunt 17's dual-EMA
> filter at 4H, scaled to 15m cadence with appropriate filter
> selection.
>
> The rules, designed to fire approximately 50-100 trades per year:
>
> **Instrument:** BTC/USDT spot on Binance.
> **Timeframe:** 15-minute bars.
> **Direction:** long only.
>
> **Entry:** Go long when ALL FOUR of the following hold on the close
> of a 15m bar:
> 1. The close price equals or exceeds the highest close of the
>    preceding 48 bars (12-hour breakout).
> 2. The close price is above the 200-period EMA of close (~50 hour
>    trend confirmation).
> 3. The current UTC hour is between 8 and 22 inclusive (active
>    European + US session liquidity hours; avoid the Asian dead
>    zone where BTC tape is thinnest).
>
> The combined filter ensures we only enter when there's a genuine
> 12-hour breakout, the medium-term trend is bullish, AND we're in
> high-liquidity hours where fills are more reliable.
>
> Entry fires on bar close (signal); fill on the next-bar open at
> realised slippage.
>
> **Exit:** Two paths, whichever fires first:
> 1. The close price drops below the 200-period EMA of close.
> 2. A hard stop-loss at 3 × ATR(14) below the entry price.
>
> **Position sizing:** 1% of equity per trade, fixed percent.
>
> **Expected trade frequency:** approximately 50-100 round-trip
> trades per year on BTC/USDT 15m. The 4-filter discipline reduces
> the firing rate from generic 15m-breakout levels (~500-1000/year)
> down to weekly-or-less cadence.
>
> **Author claim:** the same confirmation-layered shape that produced
> Sharpe 1.0+ on BTC 4H (Hunts 17/19/20) is expected to produce a
> similar or slightly degraded Sharpe at 15m IF the underlying
> structural property (multi-filter agreement = cleaner signal) holds
> across cadence. Conservative estimate: Sharpe 0.7-0.9, max drawdown
> 1-3% at 1% sizing.

## Expected schema shape

- `instrument.symbol = "BTC/USDT"`, asset_class = "crypto_spot"
- `primary_timeframe = "15m"`, direction = "long"
- `entry.condition` = AND(
    `compare(close >= lagged(highest(close, 48), 1))`,
    `compare(close > EMA(close, 200))`,
    `TimeOfDayCondition(start_hour_utc=8, end_hour_utc=22, inclusive_end=true)`
  )
- `exit.exits` = [
    `compare(close < EMA(close, 200))` condition exit,
    `StopLossAtrMultiple(atr_period=14, mult=3.0)`
  ]

## Attribution

Operator-written diagnostic candidate. Restrictive multi-filter design
inspired by Hunt 17's dual-EMA approach and v1.2.C's TimeOfDayCondition
primitive. Designed specifically to test whether confirmation-layer
discipline scales to 15m cadence on BTC.
