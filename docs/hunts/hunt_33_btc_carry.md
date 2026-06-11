# Hunt 33 — BTC perp PURE CARRY (funding harvest) probe (E.6)

- **Source:** operator synthesised, pure carry factor probe, E.6
- **Date:** 2026-06-06
- **Asset:** BTC/USDT perpetual (Binance USDM), 4H, 2020-01-01 → 2026-05-31 (6.4y, 14,058 bars)
- **Engine:** new `workers/.../backtest/perp_carry.py` (funding-driven entry/exit) — needed a small carry-signal engine extension (reuses the verified E.3 funding-on-mark accounting).
- **Verdict: REJECT** — composite **57.81 / mixed_signals**, cost-sanity **FAIL**. Primary driver: **cost-eaten** (funding receipts are thinner than the round-trip cost), with a secondary fat-tail steamroller.

## The factor (pure carry — no trend)
Signal = the funding rate. Enter when funding is extreme (|z| ≥ entry_z over a rolling window of the 8h funding observations), take the **receiving side** (`direction = −sign(funding)`: SHORT when funding high/positive = crowded longs pay; LONG when low/negative). Exit when funding normalises (|z| ≤ exit_z) **or** an ATR stop on the price leg (the **steamroller cap** — bounds the adverse-price loss while holding carry). Unlevered, funding on mark.

## Cost-sanity (done first — the heart of carry)
35 trades/yr × 30 bps single-leg round-trip = **10.6%/yr cost drag**. Per trade: **funding collected +8.5 vs cost 26.3 → funding does NOT clear the round-trip** (cost ≈ 3× the receipt). BTC funding is the thinnest of the 7 assets (mean |f| 1.29 bp, p95 4.9 bp), so each carry trade harvests too little to pay for getting in and out. Cost-eaten *before* the steamroller even matters.

## Diagnostic
| metric | value |
|---|---|
| verdict / composite | **mixed_signals / 57.81** → REJECT |
| trades / freq | 226 / **35 per yr** |
| net return | **−49.6%** / Sharpe **−0.27** / max DD −64.3% |
| **funding collected** | **+1924** (positive — signal works, 155/226 trades collect) |
| **price PnL** | **−953** (steamroller exposure, ATR-stop-capped; 32 stops) |
| **cost** | **−5933** (the killer — 3× funding) |
| net | −4961 |
| walk-forward | degradation invalid (IS ≤ 0), OOS+ **2/6** |
| sweep peakiness | 0.000 (baseline a trough; best cell +60% — fragile, not robust) |
| Monte-Carlo (price-permute) | **p = 0.46** — real ≈ synthetic; price ordering barely matters, cost dominates both |
| DSR prob_real | 0.000 |

## Steamroller decomposition (headline)
- **funding +1924 / price −953 / cost −5933.** The signal genuinely harvests funding from the crowded side; the price leg is only mildly negative because the ATR stop works (worst single trade: SHORT collecting +5.8 funding, price −427.6, stopped at NET −440).
- **Fat-tail:** the 5 worst price moves net **−6146**; the *remaining 221 trades* net **+1185**. So a handful of steamrollers carry most of the price risk — but the **stop already capped aggregate price damage to −953**, so the steamroller is *contained*, not the primary killer.
- **The primary killer is COST, not the steamroller.** Funding is real but thin; the round-trip eats it 3:1. MC p=0.46 confirms: destroying the price ordering doesn't help (real ≈ synthetic) — cost dominates regardless of price luck.

## Empirical trades (engine sane)
- `[normalize]` SHORT 2020-01-31→02-02: funding +25.9, price +68.5, cost −14.9 → NET +79.5 (collected funding from crowded longs, price drifted favourably). 
- `[stop]` SHORT 2020-07-26→07-27: funding +5.8, price −427.6 (a rip-up), cost −18.3 → NET −440 (steamroller, stop fired and capped it). 
Both confirm: the strategy takes the receiving side and the stop bounds the steamroller — the verdict rests on honest behaviour.

## Verdict + recommendation
**Pure funding-carry does NOT have standalone edge on BTC at our rigor — cost-eaten.** It is structurally *uncorrelated* with trend (its driver is funding flow + a capped price leg, not price direction), so the multi-factor *idea* is sound — but carry is not a *viable* second factor here because BTC funding (~1bp) is too thin to clear 30 bps.

Distinguishing "doesn't work on BTC" from "doesn't work at all": the driver is **cost vs funding-magnitude**, which is asset-specific. BTC has the thinnest funding of the 7; SOL (mean |f| 2.28 bp), AVAX (1.90), XRP (1.84), BNB (1.79) have **fatter, tail-ier funding** that *might* clear the round-trip — but they also carry larger steamroller risk. So:
1. **Carry is not bankable as-is.** Do not seed.
2. **One targeted follow-up is justified:** the fattest-funding liquid alt (SOL) — to test whether richer funding clears cost where BTC's doesn't. Temper expectations: even SOL's ~2.3bp over a typical hold is marginal vs 30 bps, and alt steamroller risk is higher.
3. If the alt probe also rejects, the carry factor is closed and the multi-factor frontier narrows to: trend (the lone survivor) + possibly slow mean-reversion (the next untested factor).
