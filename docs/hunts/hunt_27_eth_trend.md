# Hunt 27 — ETH/USDT Perp 4H Triple-EMA Cascade (10/30/60) — Long+Short vs Long-Only

**Phase:** E.5b — multi-asset slow-trend port (perp pairs)
**Symbol:** ETH/USDT-PERP
**Timeframe:** 4H
**Asset class:** crypto_perp
**Source type:** operator synthesised, multi-asset slow-trend port, E.5b
**Date:** 2026-06-06
**Strategy:** Triple-EMA cascade 10/30/60 4H perp, tested in long+short and long-only modes, ATR-trailing stop, funding-on-mark accounting

## Strategy

Triple-EMA cascade: entry when fast EMA(10) > medium EMA(30) > slow EMA(60) (long) or the mirror (short). Exit on ATR(14)-trailing stop. Perpetual swap accounting: PnL computed on mark price; 8-hour funding accrued on open notional at each funding interval. The same shape as the BTC seed (Hunt 26, if applicable) ported to ETH.

## Cost-Sanity (pre-result)

- Long+short mode: ~44.6 trades/yr x 30 bps single-leg (60 bps round-trip) = **13.4% / yr drag** — structurally very high
- Long-only mode: ~22.9 trades/yr x 30 bps = **6.9% / yr drag** — borderline survivable at this cadence

## Diagnostic Table

| Metric | Long+Short | Long-Only |
|---|---|---|
| **Verdict** | mixed_signals | mixed_signals |
| **Composite** | 60.00 | 46.39 |
| **Sharpe** | 0.184 | 0.842 |
| **Trades/yr** | 44.6 | 22.9 |
| **Cost-sanity** | FAIL | PASS |
| **WF OOS positive** | 1/6 | 3/6 |
| **MC p-value** | 0.15 | 0.00 |
| **DSR prob_real** | 0.000 | 0.001 |
| **Net funding** | -$4,826 | -$16,637 |
| **Long net PnL** | +$19,874 | +$52,245 |
| **Short net PnL** | -$17,641 | N/A |
| **seed** | false | false |

## Mode Verdicts

### Long+Short — REJECT

Cost-sanity FAIL (13.4%/yr drag). Sharpe collapses to 0.18 vs long-only's 0.84. Short legs individually lose $17,641 net, almost fully erasing long-leg gains of $19,874. Walk-forward OOS positive windows: only 1/6 — no persistence. MC p-value 0.15 (not significant). DSR prob_real 0.000.

### Long-Only — REJECT

Despite cost-sanity PASS and a respectable Sharpe of 0.84, the strategy fails on statistical robustness: MC p-value is 0.00 but DSR prob_real is only 0.001 (below the 0.01 threshold for seeding). Walk-forward OOS positive: 3/6 — barely above chance. Composite 46.39 is below the seed floor. Funding drag on longs is heavy (-$16,637 net over 6.4 years). Verdict: mixed_signals, not likely_robust.

## Long-vs-Short Conclusion

Short legs are consistently destructive on ETH perp at 4H: they add cost drag (+44.6 vs 22.9 trades/yr), lose net ($17,641), and reduce Sharpe from 0.84 to 0.18. ETH short-side trend following at this cadence is a cost and signal-noise trap. Even long-only does not survive statistical scrutiny — DSR prob_real too low, WF OOS persistence too weak. ETH does not port the BTC slow-trend shape cleanly at this parameter set.

## Primary Driver of Rejection

`no_edge` — neither mode seeds. Long-only has a surface Sharpe but fails DSR/WF robustness checks (composite 46.39 < seed floor). Long+short is additionally destroyed by short-leg drag, compounding the rejection. The edge, if present in ETH trend at 4H, is too marginal to survive the gauntlet's statistical filters.
