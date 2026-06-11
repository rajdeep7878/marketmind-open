# Hunt 28 — SOL/USDT Perp 4H Triple-EMA Slow Trend (E.5b)

**Date:** 2026-06-06
**Source:** operator synthesised, multi-asset slow-trend port, E.5b
**Asset:** SOL/USDT perpetual swap (Binance USDM, 4H bars)
**Period:** 2020-09-14 → 2026-05-31 (5.7 years, 12 515 bars)

---

## Strategy

Triple-EMA cascade (10 / 30 / 60) on 4H perpetual-swap candles.

- **Entry long:** EMA10 > EMA30 > EMA60 (full bull stack)
- **Entry short (long+short mode):** EMA10 < EMA30 < EMA60 (full bear stack)
- **Exit:** ATR-trailing stop on mark price
- **Costs:** 30 bps per leg (maker/taker + slippage, Binance USDM)
- **Funding:** accrued on mark price at 8h intervals

---

## Cost-sanity pre-check

| Mode | Trades/yr | Round-trip cost (bps) | Annual cost drag |
|---|---|---|---|
| long+short | 42.9 | 30 (x2 legs) | ~12.9 % — **FAIL threshold is > 0 net after drag** |
| long-only | 22.1 | 30 | ~6.6 % |

Both modes pass cost-sanity (PASS flag from harness) — the long-only drag is 6.6 %/yr, long+short 12.9 %/yr, which the strategy's raw edge must absorb. The composite and Sharpe in both cases remain positive but sub-threshold.

---

## Results — both modes

| Metric | long+short | long-only |
|---|---|---|
| Verdict | mixed_signals | mixed_signals |
| Composite | 53.19 | 52.65 |
| Sharpe | 0.535 | 0.563 |
| Trades / yr | 42.9 | 22.1 |
| Cost sanity | PASS | PASS |
| WF OOS pos | 1 / 6 | 1 / 6 |
| MC p-value | 0.06 | 0.28 |
| DSR prob_real | 0.00 | 0.00 |
| Net funding ($) | -8 296 | -5 918 |
| Long net ($) | +27 014 | +20 145 |
| Short net ($) | -6 610 | n/a |

---

## Verdict

**long+short: REJECT (seed=false)**
**long-only: REJECT (seed=false)**

Both modes fail the gauntlet. The dominant failure across both is the walk-forward test: only 1 of 6 OOS windows is positive — a strong out-of-sample instability signal. DSR probability of a real edge is 0.00 in both cases, meaning the observed Sharpe is consistent with noise even at this 5.7-year sample length. The long+short MC p-value (0.06) is borderline, but DSR=0.00 overrides it; the long-only MC p-value (0.28) is firmly non-significant.

**Short legs drag:** the short book loses $6 610 net while long books earn $27 014 (long+short) / $20 145 (long-only). Funding is negative in both modes (SOL perp funding is chronically positive, i.e. shorts pay longs, hurting the short leg). However, even removing the short leg does not rescue the strategy — long-only also rejects.

**Primary driver: no_edge** — neither mode seeds; the walk-forward collapse (1/6) and DSR=0.00 indicate the strategy has no durable edge in SOL over this period, irrespective of short-leg drag.

---

## Long-vs-short conclusion

The short book underperforms the long book materially (−$6 610 vs +$27 014 for long+short) and adds funding cost. For SOL, the short-overlay destroys value relative to long-only. But long-only also rejects; the triple-EMA cascade does not produce a robust slow-trend edge on SOL at 4H, likely because SOL trends are noisier and more regime-dependent than BTC/ETH over this data window.

**Disposition:** REJECT both modes. Not forwarded to seed pool.
