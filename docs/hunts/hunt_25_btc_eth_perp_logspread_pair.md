# Hunt 25 — BTC/ETH market-neutral perp log-spread mean-reversion (Phase E.4)

- **Source:** operator-synthesised, market-neutral pair MR, E-phase (the first
  multi-leg / market-neutral hunt; no literature transcript — direct spec).
- **Date:** 2026-06-06
- **Branch:** `v2-phase-e-perp-pairs`
- **Engine:** E.3 perp-pair simulator (`workers/.../backtest/perp_pairs.py`),
  funding-on-mark verified. Multi-leg is iterative-only by construction (vbt
  `from_signals` is single-asset) → **drift-parity N/A for the multi-leg path**,
  expected, not a gap.
- **Verdict: REJECT** — composite **66.25 / likely_overfit**. Primary driver:
  **(a) no edge** (no gross reversion edge — anti-edge at the extremes),
  compounded by a fatal ~41%/yr doubled-pair cost.

## The strategy
Long ETH-perp / short BTC-perp (and the inverse), unlevered
(`leg_A + leg_B == percent·equity`, gross ≤ equity). Signal: z-score of the
log-spread `log(ETH) − log(BTC)` over 168×1h (7d). ENTER at |z| ≥ entry_z=2.5,
EXIT (revert) at |z| ≤ exit_z=0.5, STOP (divergence) at |z| ≥ stop_z=4.0,
correlation regime gate (corr_period 168, corr_min 0.3). Funding modelled on
mark, both legs (E.3, verified). Data: E.2 fixtures, 56,232 contiguous 1h
bars/leg, identical grid, 2020-01-01 → 2026-05-31.

## Cost-sanity (pre-hunt; the load-bearing doubled-pair number)
Pair round-trip = **60 bps** (2 legs × ~30 bps conservative crypto_perp).
`drag = trades/yr × 60bps`: 10/yr→6%, 25/yr→15%, 50/yr→30%, 100/yr→60%.
**Realized: 68 trades/yr → 40.9%/yr cost drag.** A pair MR claiming an edge must
clear this; BTC/ETH is the most-arbitraged crypto pair, so the gross edge is
assumed thin — the cost-sanity flagged this as a likely fail before the hunt.

## Diagnostic
| metric | value |
|---|---|
| verdict / composite | **likely_overfit / 66.25** (REJECT) |
| trades / freq | 438 / **68 per yr** (genuinely "fast") |
| net return | **−89.3%** over 6.4y |
| GROSS (cost=0) incl funding | **−48%** (price −51%, funding +3%) |
| annual cost drag @ 60bps | **40.9%/yr** |
| cost-sanity ratio | **FAIL** (gross −48% < drag) |
| net funding | **+255 (+3%, small CREDIT)** — not the driver |
| walk-forward | degradation invalid (IS_avg ≤ 0), **OOS+ 0/6** |
| parameter sweep | peakiness 0.000 (whole 27-cell grid loses; best −74%) |
| Monte-Carlo | **p = 1.000** (real does WORSE than every shuffle) |
| DSR prob_real_v2 | **0.000** |
| exit mix | 268 reversion (avg price −1.1), 170 stop (avg price **−28.3**) |

## Primary driver — (a) NO EDGE
Even **gross (cost zeroed), the strategy loses ~48%.** The MR thesis is simply
wrong for this pair at bar scale: at |z| ≥ 2.5σ the spread *continues diverging*
more often than it reverts — 170 stop-outs average −28.3 each while the 268
"reversions" barely break even (−1.1 avg). MC p=1.000 confirms the real ordering
is worse than random — there is no exploitable temporal reversion structure. The
40.9%/yr doubled cost then makes a no-edge strategy catastrophic, but it is
**not** "real-edge-but-cost-eaten" — there is no gross edge to eat.

## Empirical round-trip (honesty check — engine behaves sanely)
2020-01-08 long-spread entry at z=−3.60 (ETH 145.64 / BTC 8376.67); 16 funding
accruals, sign-correct on mark, net ~0; reversion exit 2020-01-10 at z=−0.29;
PnL price +68.82 / funding +0.00 / cost −14.24 → **net +54.58**. Over the hold
ETH −2.54% and BTC −4.91% (market fell) yet PnL came from the **+2.37% spread
convergence** — genuinely market-neutral. The engine's verified honesty
translates to sane trades; the REJECT rests on real behavior, not an artifact.

## Conclusion + E.5
Market-neutral BTC/ETH log-spread MR is **fast (68/yr) but has NO edge** at our
rigor — the most-arbitraged crypto pair leaves nothing for a retail bar-based
strategy, and the doubled cost is independently fatal. This is **not** the
fast-with-edge answer; it joins fast trend / fast MR / equity ORB as a no-edge
dead-end. The only survivors remain the SLOW BTC 4H trend cluster (Sharpe
0.66-1.08). Because the driver is (a) no-gross-edge (not cost-eaten-real-edge),
a cheaper venue alone would NOT rescue it. E.5 options, grounded in the driver:
(1) test a **less-arbitraged pair** (ETH/SOL, an L1 basket) to see if residual
reversion exists where BTC/ETH has none — but only if frequency can be cut so
60bps doesn't dominate; (2) accept the cadence boundary holds for market-neutral
too and redirect to the slow regime where edge actually lives.
