# Hunt 29 — BNB/USDT 4H Perpetual Triple-EMA Cascade Slow-Trend

**Date:** 2026-06-06
**Source:** Operator synthesised, multi-asset slow-trend port, E.5b
**Asset:** BNB/USDT perpetual (Binance USDM)
**Verdict:** REJECT (both modes)

---

## Strategy

Triple-EMA cascade (EMA 10 / 30 / 60) on the 4H perpetual bar series. Long entry when EMA10 > EMA30 > EMA60 (full bullish cascade); short entry (long+short mode only) when EMA10 < EMA30 < EMA60. ATR-trailing stop manages exits. Funding accrues on mark price each 8H settlement; PnL marked to `mark_close` intrabar.

- Timeframe: 4H
- Indicators: EMA 10, 30, 60 cascade confirmation
- Stop: ATR-trailing
- Costs: 30 bps round-trip per leg (Binance perp taker), funding-on-mark
- Period: 2020-02-10 to 2026-05-31 (6.3 years, 13 816 bars)

---

## Cost-sanity pre-check

| Mode | Trades/yr | Round-trip cost (bps) | Annual cost drag |
|---|---|---|---|
| Long+short | 43.4 | 30 | 13.0 % — **FAIL** |
| Long-only | 23.6 | 30 | 7.1 % — **PASS** |

Long+short generates ~2x the trade frequency, pushing cost drag above the structural kill threshold. Long-only passes the raw cost gate but still faces gauntlet scrutiny.

---

## Both-modes diagnostic table

| Metric | Long+short | Long-only |
|---|---|---|
| Verdict | mixed_signals | mixed_signals |
| Composite | 59.84 | 58.75 |
| Sharpe | -0.254 | 0.236 |
| Trades/yr | 43.4 | 23.6 |
| Cost-sanity | FAIL | PASS |
| WF OOS positive | 3/6 | 4/6 |
| MC p-value | 0.59 | 0.52 |
| DSR prob_real | 0.000 | 0.000 |
| Net funding | -376 USD | -233 USD |
| Long net PnL | -764 USD | +3 487 USD |
| Short net PnL | -5 811 USD | 0 (n/a) |

---

## Seed / Reject

| Mode | Seed? | Reason |
|---|---|---|
| Long+short | REJECT | cost-sanity FAIL; Sharpe -0.25; DSR 0.000; short legs deeply negative |
| Long-only | REJECT | DSR 0.000; MC p-value 0.52 (not significant); WF 4/6 below threshold; no statistical robustness |

---

## Long-vs-short conclusion

The short leg destroys value: short_net = -5 811 vs long_net = -764 in long+short mode. Even the long side struggles directionally on BNB at 4H cadence — the triple-EMA cascade does not find a clean slow trend to ride on this asset. Long-only produces a small positive gross PnL (+3 487 USD long_net) but after 7 % annual cost drag, and with a DSR of 0.000 and MC p-value 0.52, there is no statistically credible edge — the result is indistinguishable from noise.

BNB does not port this slow-trend template. The asset's volatility regime and mean-reverting episodes between trend bursts defeat the EMA cascade at 4H.

---

## Primary driver

**no_edge** — neither mode seeds. MC p-value is 0.52 (long-only) / 0.59 (long+short), DSR prob_real = 0.000 across both modes. The equity curve is statistically indistinguishable from random. Short legs compound the damage but the long side alone lacks evidence of real edge.
