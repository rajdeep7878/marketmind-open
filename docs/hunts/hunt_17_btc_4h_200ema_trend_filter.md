# Hunt 17 — BTC/USDT 4H 200-EMA Trend Filter (Long-Only)

**Phase:** post-C.7 autonomous hunt session
**Symbol:** BTC/USDT
**Timeframe:** 4H
**Asset class:** crypto_spot
**Source type:** raw_text (operator-written; baseline trend-following candidate)
**Date:** 2026-05-26
**Expected primitives:** `EMA(200)`, `EMA(50)`, `compare`, `Crossover` for exit, `ATR(14)`, `StopLossAtrMultiple`

## Why this hunt

Phase C foundation complete (C.1.1-C.7). C.7's FX hunt was cost-eaten
(likely_overfit 66.07). This session widens the search across regimes.
Hunt 17 is a baseline trend-following candidate — Faber-style 200-EMA
filter adapted to BTC/USDT 4H, long-only. The mechanism is the most
heavily-tested trend-following pattern in systematic trading; if it
doesn't pass our gauntlet, the rejection itself is a meaningful
finding about retail-style trend strategies at our evaluation rigor.

## Cost-sanity (pre-extraction)

- Implied trade frequency: 6-12 round-trip/year at 4H BTC (slow filter; signals are rare)
- Per-side cost: crypto_spot = 10 bps commission + 5 bps slippage = 15 bps/side
- Round-trip cost: 15 × 2 = 30 bps
- Annual cost drag: 6-12 × 30 bps = 180-360 bps = **1.8-3.6% / year**
- Author claim: Sharpe ~1.0, returns competitive with B&H but smoother
- Cost-survivability: clear yes — claimed annualized edge of 20-40% well above 3.6% drag

## Source text

> The 200-period exponential moving average is the most widely-followed
> long-term trend filter in systematic trading. It originates in Meb
> Faber's 2007 paper "A Quantitative Approach to Tactical Asset
> Allocation" — the SPY/10-month-SMA system that has become the
> textbook example of a permanent trend-following overlay — and has
> since been adapted to nearly every liquid asset class, including
> crypto majors.
>
> The mechanism is straightforward: in a persistent uptrend, prices
> spend most of their time above the long EMA, and the EMA itself
> slopes upward. In a downtrend or sideways chop, prices oscillate
> around the EMA and the slope flattens or inverts. By restricting
> long entries to bars where the close is above the 200-EMA AND the
> 50-EMA is above the 200-EMA, the strategy avoids both bear-market
> drawdowns and range-bound whipsaws, at the cost of being late to
> new uptrends and giving back some open profit on the way out. This
> is the classic trend-following trade-off: many small losses, few
> large wins, positive expectancy from the right tail.
>
> The exact rules, adapted from Faber's framework to BTC/USDT on a
> 4-hour timeframe:
>
> **Instrument:** BTC/USDT spot on Binance, the most liquid crypto pair.
>
> **Timeframe:** 4-hour bars. The 200-EMA on 4H corresponds to roughly
> 33 days of price history — a slow enough filter to define the
> medium-term trend, fast enough to react within a month when the
> trend genuinely changes.
>
> **Direction:** long only. The short side is intentionally excluded.
> BTC has a persistent long-term upward drift since inception, and
> most "BTC short" backtests look strong on in-sample data but degrade
> catastrophically out-of-sample.
>
> **Entry:** Go long when BOTH of the following hold on the close of a
> 4H bar:
> 1. The close price is above the 200-period EMA of close.
> 2. The 50-period EMA of close is above the 200-period EMA of close.
>
> The combined filter ensures we only enter when both price and the
> medium-term trend agree with the long-term trend. Entry fires on
> bar close (signal); fill on the next-bar open at realised slippage.
>
> **Exit:** Two paths, whichever fires first:
> 1. The close price crosses BELOW the 200-period EMA of close
>    (trend break).
> 2. A hard stop-loss at 3 × ATR(14) below the entry price
>    (flash-crash protection; this is the primitive that makes the
>    strategy seedable in MarketMind's paper trader, per the project's
>    "every entry needs a protective stop" requirement).
>
> **Position sizing:** 1% of equity per trade, fixed percent.
>
> **Expected trade frequency:** approximately 6 to 12 round-trip
> trades per year on 4H BTC, depending on how choppy the underlying
> tape is. This is well within the cost-survivability envelope for
> BTC/USDT at Binance spot (~30 bps round-trip = ~360 bps annual drag
> at 12 trades), against a long-run buy-and-hold benchmark return of
> roughly 60-80% annualised across the 2017-2024 window.
>
> **Author claim:** on the BTC/USDT 4H series from 2018-01-01 through
> 2024-12-31, the Sharpe ratio of this system is approximately 1.0,
> with a max drawdown of roughly 35% (versus buy-and-hold's max
> drawdown of roughly 80% in the 2022 bear market). The system is
> expected to underperform buy-and-hold in raw return during strong
> sustained bull runs (it gives back the top 15-25% of every leg by
> waiting for the 200-EMA cross to exit) but to substantially
> outperform on risk-adjusted measures by sidestepping the deep
> bear-market drawdowns.
>
> The strategy is not original — variants have been published by
> Faber (2007 SPY/SMA), Carver (2015 "Systematic Trading", chapters
> on trend-following carry overlays), and the BTC-quant community
> on Reddit / Twitter throughout the 2017-2024 cycle. It is
> included here as a baseline trend-following candidate for
> systematic evaluation, not as a novel edge.

## Expected schema shape

- `instrument.symbol = "BTC/USDT"`, asset_class = "crypto_spot"
- `primary_timeframe = "4h"`
- `direction = "long"`
- `entry.condition` = AND(`compare(close > EMA(close, 200))`, `compare(EMA(close, 50) > EMA(close, 200))`)
- `exit.exits` = [`Crossover(close, EMA(close, 200), direction="below")` condition exit, `StopLossAtrMultiple(atr_period=14, mult=3.0)`]
- `position_sizing` = fixed_percent 1%
- `costs` = crypto defaults

## Attribution

Operator-written, grounded in Faber 2007 + Carver 2015 + the broader
trend-following literature. No specific paste from a copyrighted
source; the mechanism is industry-standard.
