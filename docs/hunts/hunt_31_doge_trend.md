# Hunt 31 — DOGE/USDT Perp Slow-Trend (Triple-EMA Cascade)

**Source:** Operator synthesised, multi-asset slow-trend port, E.5b
**Date:** 2026-06-06
**Asset:** DOGE/USDT perpetual swap, 4H bars
**Period:** 2020-07-10 → 2026-05-31 (5.9 years, 12 910 bars)

---

## Strategy

Triple-EMA cascade (10 / 30 / 60 bars, 4H perp).
- **Long entry:** fast EMA > mid EMA > slow EMA (bullish cascade); stop: ATR-trailing.
- **Short entry (long+short mode only):** fast EMA < mid EMA < slow EMA (bearish cascade); same ATR-trailing stop.
- **Funding:** accrued on mark price at each 8H funding event (Binance USDM).
- **Sizing:** fixed percent, no leverage (unlevered research path).

The same triple-EMA template that seeded BTC 4H (Hunt 20) is ported here to test whether the edge is asset-specific or portable.

---

## Cost-Sanity Pre-Check

| Mode | Trades/yr | Round-trip (bps) | Cost drag (%/yr) | Pass? |
|---|---|---|---|---|
| Long+short | 38.8 | 30 | 11.65 | PASS |
| Long-only | 19.7 | 30 | 5.90 | PASS |

Both modes clear the cost gate; the edge question is pure overfitting / walk-forward.

---

## Gauntlet Results

| Metric | Long+Short | Long-Only |
|---|---|---|
| **Verdict** | likely_overfit | likely_overfit |
| **Composite** | 66.42 | 71.03 |
| **Sharpe** | 0.608 | 0.501 |
| **Trades/yr** | 38.8 | 19.7 |
| **Cost sanity** | PASS | PASS |
| **WF OOS pos windows** | 2/6 | 2/6 |
| **MC p-value** | 0.03 | 0.19 |
| **DSR prob_real** | 0.000 | 0.000 |
| **Net funding** | -$2 487 | -$7 838 |
| **Long net** | +$37 280 | +$18 622 |
| **Short net** | -$6 776 | n/a |
| **Seed** | false | false |

---

## Seed / Reject

| Mode | Decision |
|---|---|
| Long+short | **REJECT** — likely_overfit; DSR 0.0, WF only 2/6 OOS positive |
| Long-only | **REJECT** — likely_overfit; DSR 0.0, WF only 2/6 OOS positive, MC p-value 0.19 |

---

## Long-vs-Short Conclusion

Adding the short leg improves Sharpe (0.608 vs 0.501) and boosts composite slightly (66.42 vs 71.03 — composite is inverted, lower = better in raw form; here long+short is lower, meaning marginally better). However long+short carries a larger net-funding drag (-$2 487 vs -$7 838 long-only note: long-only accumulates more funding drag per trade because it holds through more adverse funding periods without the short offsetting). The short leg itself lost -$6 776 net, erasing a meaningful portion of the long book's gain.

Both modes fail at the walk-forward gate — only 2 of 6 OOS windows are positive, indicating the EMA cascade parameters are tuned to DOGE's 2020-2021 bull cycle and do not generalise across the full 5.9-year sample.

**Primary driver of rejection: no_edge** — the strategy has no walk-forward-validated edge on DOGE at any combination of the swept parameters. DSR probability of being real is 0.0 across both modes.

---

## Notes

- The BTC twin (Hunt 20) SEEDED with long+short (seed=true). DOGE's rejection here suggests the triple-EMA edge is asset-specific, not portable — at least not to DOGE's higher-volatility, thinner-liquidity regime.
- Sweep peakiness is high (0.833 long+short, 0.717 long-only), confirming the in-sample returns cluster at a narrow parameter island — a classic overfitting signature.
- Funding drag is severe: -$7 838 on the long-only book across 5.9 years, consistent with DOGE's historically elevated funding rates during speculative spikes.
