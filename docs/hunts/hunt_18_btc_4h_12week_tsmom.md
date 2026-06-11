# Hunt 18 — BTC/USDT 4H 12-Week Time-Series Momentum (Long-Only)

**Phase:** post-C.7 autonomous hunt session
**Symbol:** BTC/USDT
**Timeframe:** 4H
**Asset class:** crypto_spot
**Source type:** raw_text (operator-written; based on Moskowitz/Ooi/Pedersen 2012 adapted to BTC)
**Date:** 2026-05-27
**Expected primitives:** `Returns(period=84)` OR `Compare(close > Lagged(close, 84))`, `ATR(14)`, `StopLossAtrMultiple`

## Why this hunt

Hunt 17 (200-EMA trend filter) passed the gauntlet at likely_robust
16.19 — a baseline trend-following signal. Hunt 18 tests a DIFFERENT
trend-following formulation: time-series momentum at a fixed lookback
(12 weeks = 504 hours = 126 bars at 4H), per Moskowitz/Ooi/Pedersen
2012 ("Time Series Momentum", JFE). The mechanism is distinct: TSMOM
fires when the trailing N-bar return is positive, regardless of
indicator crossover state. This is a parameter-free entry signal —
no period optimization decisions, just one lookback length.

The hunt tests whether parameter-simpler trend signals also pass the
gauntlet (Hunt 17 used two EMAs at 50 + 200; Hunt 18 uses just one
lookback period). If both pass, trend signals on BTC have genuine
edge. If only Hunt 17 passes, the EMA-cross specificity matters.

## Cost-sanity (pre-extraction)

- Implied trade frequency: 4-8 round-trip/year at 4H BTC with 12-week
  lookback (signals are very sticky — once positive, they stay positive
  for months at a time during trends)
- Per-side cost: crypto_spot = 15 bps/side, round-trip = 30 bps
- Annual cost drag: 4-8 × 30 bps = 120-240 bps = **1.2-2.4% / year**
- Author claim: long-only TSMOM Sharpe ~0.4-0.7 in equities/commodities
  per Moskowitz et al; BTC variants in practitioner community report
  Sharpe ~1.0 due to BTC's strong trend persistence
- Cost-survivability: clear yes — annual drag <3% vs claimed edge >10%

## Source text

> Time-series momentum is one of the most robust documented anomalies
> in cross-asset trading. Moskowitz, Ooi, and Pedersen's 2012 Journal
> of Financial Economics paper "Time Series Momentum" tested a simple
> trading rule across 58 instruments (equity index futures, currency
> forwards, commodity futures, bond futures) using monthly data:
> if the past 12-month return of an asset is positive, hold it long
> for the next month; if negative, hold it short. The rule produced
> Sharpe ratios of 0.4-0.7 in every asset class tested over four
> decades of data, with low correlation to traditional benchmarks.
>
> Adapting this to crypto: the BTC/USDT pair has shown even stronger
> time-series momentum than traditional assets, in part because of
> BTC's secular adoption trend and the high autocorrelation of crypto
> regimes (4-6 month bull/bear cycles rather than month-by-month
> oscillations). The 12-week (≈3 month) lookback corresponds to one
> full crypto-cycle quarter, capturing meaningful trend changes
> without overreacting to weekly noise.
>
> The rules, adapted to BTC/USDT 4H bars (long-only variant — the
> short side adds tail-risk that erodes the Sharpe gain):
>
> **Instrument:** BTC/USDT spot on Binance.
>
> **Timeframe:** 4-hour bars. 12 weeks of 4H bars = 504 bars (more
> precisely, the daily-equivalent measure is 12 × 5 × 6 = 360 4H
> bars on a 24/5 calendar; on crypto 24/7 it's 12 × 7 × 6 = 504 4H
> bars). We use 504 to match crypto's continuous-trading reality.
>
> **Direction:** long only.
>
> **Entry:** Go long when the close price exceeds the close price
> exactly 504 bars ago (12 weeks of 4H bars). This is the canonical
> time-series momentum signal: trailing 12-week return is positive
> ⟺ close[t] > close[t-504]. No indicator, no smoothing, just a
> direct price comparison with a lagged reference.
>
> **Exit:** Two paths, whichever fires first:
> 1. The close price drops BELOW the close price 504 bars ago
>    (trailing 12-week return turns negative).
> 2. A hard stop-loss at 3 × ATR(14) below the entry price.
>
> Both exits are time-series momentum exits — the first is the spec
> exit (signal flips), the second is the protective stop for
> flash-crash protection.
>
> **Position sizing:** 1% of equity per trade, fixed percent.
>
> **Expected trade frequency:** approximately 4 to 8 round-trip
> trades per year on BTC/USDT 4H. The signal is sticky — once the
> 12-week return turns positive, it tends to stay positive for
> months, then flip to negative for months. The trade count is
> dominated by these regime shifts, not by intra-regime noise.
>
> **Author claim:** Moskowitz/Ooi/Pedersen 2012 documented Sharpe
> ratios of 0.4-0.7 in equity index futures, currency forwards, and
> commodity futures using a 12-month lookback on monthly data. For
> BTC specifically, applying a 12-week lookback on 4H bars (the
> crypto-cycle-adjusted variant), backtests across 2018-2024 produce
> Sharpe ratios of 0.8-1.2 with max drawdowns of 25-40%, primarily
> capturing the long arc of each bull-market leg while sidestepping
> the deep bear-market drawdowns of 70-80%.
>
> The hypothesis: if 12-week TSMOM has real edge in BTC, it should
> survive the gauntlet's walk-forward + parameter-sweep + monte-carlo
> + deflated-Sharpe tests with `likely_robust` verdict. If not, the
> historical Sharpe numbers were data-mined — a common failure mode
> for "single-indicator" backtests in crypto.

## Expected schema shape

- `instrument.symbol = "BTC/USDT"`, asset_class = "crypto_spot"
- `primary_timeframe = "4h"`, direction = "long"
- `entry.condition` = `compare(close > lagged(close, 504))` — direct price comparison
- `exit.exits` = [`compare(close < lagged(close, 504))` condition exit, `StopLossAtrMultiple(atr_period=14, mult=3.0)`]
- `position_sizing` = fixed_percent 1%
- `costs` = crypto defaults

## Attribution

Operator-written, grounded in Moskowitz/Ooi/Pedersen 2012 JFE. The
BTC adaptation is operator-applied (the original paper does not
include BTC). The 12-week lookback is the crypto-cycle adaptation
of the paper's 12-month lookback on monthly data.
