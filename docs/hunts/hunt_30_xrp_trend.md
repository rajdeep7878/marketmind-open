# Hunt 30 — XRP/USDT Perp 4H Triple-EMA Cascade Slow Trend

**Date:** 2026-06-06
**Source:** Operator synthesised, multi-asset slow-trend port, E.5b
**Asset:** XRP/USDT Perpetual (Binance USDM)
**Timeframe:** 4H
**Hunt period:** 2020-01-06 → 2026-05-31 (6.4 years, 14026 bars)

---

## Strategy

Triple-EMA cascade (10 / 30 / 60 bars) on 4H XRP perp. Entry when all three EMAs are stacked
(EMA10 > EMA30 > EMA60 for long; EMA10 < EMA30 < EMA60 for short). ATR-based trailing stop.
Funding accrual on mark price. Tested in two directional modes:

- **Long+short** — trades both sides of the EMA cascade.
- **Long-only** — longs only; shorts are skipped.

Cost model: 30 bps per single leg (Binance perp taker fee + slippage), same rate as BTC/ETH
perp anchors.

---

## Cost-sanity check (pre-extraction gate)

| Mode | Trades/yr | Single-leg bps | Round-trip cost/yr |
|---|---|---|---|
| Long+short | 43.3 | 30 bps | ~12.98 % / yr |
| Long-only | 22.3 | 30 bps | ~6.70 % / yr |

Both modes fail the cost-sanity gate: 43.3 and 22.3 trades/yr at 30 bps each is too expensive
to sustain any realistic edge at 4H frequency.

---

## Diagnostic results

| Metric | Long+short | Long-only |
|---|---|---|
| Verdict | likely_overfit | likely_overfit |
| Composite score | 61.56 | 66.25 |
| Sharpe | -0.357 | -0.924 |
| Trades / yr | 43.3 | 22.3 |
| Cost-sanity | FAIL | FAIL |
| WF OOS positive | 1/6 | 1/6 |
| MC p-value | 0.70 | 1.00 |
| DSR prob_real | 0.00 | 0.00 |
| Net funding (USD) | -1489 | -1917 |
| Long-leg net (USD) | -10683 | -8991 |
| Short-leg net (USD) | +3114 | 0 |

---

## Verdict by mode

| Mode | Seed? | Reason |
|---|---|---|
| Long+short | REJECT | likely_overfit; cost-sanity FAIL; sharpe < 0; WF 1/6; DSR 0.00 |
| Long-only | REJECT | likely_overfit; cost-sanity FAIL; sharpe -0.924; WF 1/6; DSR 0.00 |

---

## Long-vs-short conclusion

The short leg (+3114 USD net) marginally outperformed the long leg (-10683 USD net), suggesting
XRP trends bearishly over the test window or that the cascade fires more cleanly on the short
side. However, the combined picture is unambiguous: both modes are deeply negative in Sharpe,
both fail cost-sanity, and walk-forward wins only 1 of 6 out-of-sample windows. Adding the
short leg does not rescue the strategy — it merely reduces total loss.

Funding accrual is net negative in both modes (perp holders paid funding net, consistent with
a frequently-bullish XRP perpetual market across the test period).

---

## Primary driver of rejection

**no_edge** — neither mode seeds. The triple-EMA cascade that showed marginal life on BTC
(hunt 20, single REJECT on BTC itself) has no edge on XRP at 4H. MC p-value of 1.00 on
long-only confirms the returns are indistinguishable from random. Cost drag compounds the
failure but is not the primary driver; even gross returns are poor (DSR 0.00 on both modes).
