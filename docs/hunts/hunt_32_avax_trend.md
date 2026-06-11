# Hunt 32 — AVAX/USDT Perp 4H Triple-EMA Slow-Trend

**Source:** Operator synthesised, multi-asset slow-trend port, E.5b
**Date:** 2026-06-06
**Asset:** AVAX/USDT perpetual swap (Binance USDM)
**Period:** 2020-09-23 → 2026-05-31 (5.7 years, 12 461 bars)

## Strategy

Triple-EMA cascade on 4H perp candles:

- **EMAs:** 10 / 30 / 60 (short / medium / long)
- **Long entry:** EMA10 > EMA30 > EMA60 (full bull alignment)
- **Short entry (long+short mode):** EMA10 < EMA30 < EMA60 (full bear alignment)
- **Exit:** ATR-trailing stop on either leg
- **Funding:** accrued on mark price, 8H Binance USDM funding rate
- **Costs:** 30 bps round-trip per leg (taker fee + slippage), Binance USDM reference

## Cost-Sanity Check

| Mode | Trades/yr | Round-trip cost/trade | Annual cost drag |
|---|---|---|---|
| long+short | 42.4 | 30 bps | 12.71 %/yr — FAIL |
| long-only | 21.1 | 30 bps | 6.33 %/yr — FAIL |

Both modes fail the cost-sanity gate. At AVAX's trend frequency, the EMA cascade churns fast enough (especially on the short side) that fee drag swamps any gross edge.

## Diagnostic Table

| Metric | long+short | long-only |
|---|---|---|
| Verdict | mixed_signals | likely_overfit |
| Composite | 57.81 | 65.78 |
| Sharpe | 0.007 | -0.308 |
| Trades/yr | 42.4 | 21.1 |
| Cost-sanity | FAIL (12.71 %/yr) | FAIL (6.33 %/yr) |
| WF OOS positive | 2/6 | 1/6 |
| MC p-value | 0.46 | 0.97 |
| DSR prob_real | 0.000 | 0.000 |
| Net funding | -3 081 USD | -3 823 USD |
| Long net PnL | -5 330 USD | -7 664 USD |
| Short net PnL | -1 141 USD | — |
| Seed? | NO | NO |

## Verdict

**long+short: REJECT** — seed=false. Composite 57.81 is marginal, but Sharpe is near-zero (0.007), cost-sanity FAILS at 12.71 %/yr, WF OOS only 2/6, DSR=0. Net funding negative (-3 081 USD). Both legs are loss-making in isolation.

**long-only: REJECT** — seed=false. Sharpe negative (-0.308), cost-sanity FAILS at 6.33 %/yr, WF OOS collapses to 1/6, MC p-value 0.97 (near-random). DSR=0.

## Long vs Short Conclusion

Short legs (-1 141 USD net) fared marginally less badly than long legs (-5 330 USD net), but neither is profitable. AVAX trends are not captured cleanly by the 10/30/60 EMA alignment — the asset is more volatile and mean-reverting at 4H cadence than BTC/ETH, producing excessive churn on both sides. Adding shorts does not rescue the strategy; it merely spreads losses across two directions.

## Primary Driver

**no_edge** — Neither mode seeds. MC p-values are high (0.46 / 0.97), DSR prob_real = 0 for both, WF OOS window counts are below the random-baseline expectation. Cost drag is severe but it is not the *only* barrier; the gross Sharpes before cost are also poor. The triple-EMA cascade does not capture a reliable trend edge in AVAX at 4H perp resolution.
