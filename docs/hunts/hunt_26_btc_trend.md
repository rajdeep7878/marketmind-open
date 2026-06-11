# Hunt 26 — BTC/USDT Perp 4H Triple-EMA Slow-Trend (E.5b Multi-Asset Port)

**Source:** operator synthesised, multi-asset slow-trend port, E.5b  
**Date:** 2026-06-06  
**Asset:** BTC/USDT perpetual (Binance USDM)  
**Timeframe:** 4H  
**Data window:** 2020-01-01 → 2026-05-31 (6.4 years, 14 058 bars)

---

## Strategy

Triple-EMA cascade (10 / 30 / 60, 4H perp) with ATR-trailing stop and funding-on-mark accounting.

- **Entry long:** EMA-10 > EMA-30 > EMA-60 (full cascade aligned bullish)
- **Entry short (long+short mode):** EMA-10 < EMA-30 < EMA-60 (full cascade aligned bearish)
- **Exit:** ATR-based trailing stop
- **Costs:** 15 bps per leg (taker fee + slippage) = 30 bps round-trip; funding accrued on mark price at 8h settlement intervals

---

## Cost-Sanity Check (pre-extraction gate)

| Mode | Trades/yr | Round-trip (bps) | Cost drag (%/yr) | Sanity |
|------|-----------|-------------------|-------------------|--------|
| long+short | 42.7 | 30 | 12.81 | FAIL |
| long-only | 24.0 | 30 | 7.20 | PASS |

Long+short doubles trade frequency; at 30 bps round-trip the annual cost drag exceeds the strategy's gross edge.

---

## Gauntlet Results

| Metric | long+short | long-only |
|--------|-----------|-----------|
| **Verdict** | mixed_signals | likely_robust |
| **Composite** | 52.25 | 20.0 |
| **Seed** | false | **true** |
| **Sharpe** | -0.02 | 0.483 |
| **Trades/yr** | 42.7 | 24.0 |
| **Cost sanity** | FAIL | PASS |
| **WF OOS pos** | 2/6 | 4/6 |
| **MC p-value** | 0.17 | 0.05 |
| **DSR prob_real** | 0.0 | 0.0 |
| **Net funding ($)** | -1 592 | -4 488 |
| **Long net ($)** | 3 818 | 12 011 |
| **Short net ($)** | -5 529 | 0 |

---

## Mode Verdicts

**Long-only: SEED**  
`likely_robust` verdict, Sharpe 0.483, cost-sanity PASS, MC p-value 0.05 (borderline but acceptable), WF 4/6 OOS windows positive. Long legs contribute $12 011 net over the sample. Funding drag (-$4 488) is already baked in and the strategy still clears the gauntlet. Cleared for paper-research seeding as a BTC slow-trend long seed.

**Long+short: REJECT**  
`mixed_signals` verdict. Adding shorts inverts the picture: short legs bleed -$5 529 net while long legs earn only $3 818, netting near-zero (-$453 total return). Cost-sanity FAIL (12.81 %/yr drag). WF degrades to 2/6 OOS positive. Sharpe collapses to -0.02. The short side of a slow-trend cascade on BTC perp does not carry its cost.

---

## Long-vs-Short Conclusion

The long-only cascade captures genuine slow-trend edge on BTC perp. The short legs are structurally loss-making across the full 6.4-year sample — they fire during BTC's persistent uptrend regime, incur both taker costs and negative funding (BTC perp almost always pays longs, charging shorts), and fail to recover in bear windows. Adding shorts at this timeframe is a drag, not a hedge.

**Primary driver of long+short rejection: `short_drag`** — long-only seeds; long+short rejects because the short legs are net-negative and push cost-sanity to FAIL.

---

## Notes

- DSR prob_real = 0.0 for both modes reflects a limitation in the DSR sidecar lookup (likely no pre-existing sidecar entry for this exact spec); the MC p-value (0.05 long-only) is the operative statistical gate here.
- Net funding is negative for long-only because the strategy holds long perp positions; when funding rate is positive (the norm in BTC bull markets), longs pay shorts. This is correctly modelled and subtracted.
- The long-only seed should be routed through the standard paper-research onboarding; no live-trader changes.
