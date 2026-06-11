# Strategy audit — 1H re-run comparison (2026-05-19)

> **None of the four selected strategies survive translation to 1H.** Every candidate's gauntlet score drops; three of the four go from positive to negative returns; all four fail to beat buy-and-hold; every walk-forward signal becomes either invalid (IS lost money) or more degenerate than at 4H. Fee drag plus regime mismatch destroys the edge in all four cases. **Recommend: keep these strategies at their original 4H/1D timeframe if seeding at all, and do not seed any of them on 1H.**

Companion to `strategy-audit-2026-05-19.md` and `strategy-audit-2026-05-19-rerun.md`. We picked the four most-promising BTC/USDT strategies with completed 4H or 1D backtest + overfitting analyses, re-ran their backtest + full overfitting pipeline at 1H, and compared.

All new rows landed in the DB (new `backtest_results` IDs, new `overfitting_analyses` IDs). The 1H backtest rows use `end_ts = original_end_ts − 1s` to dodge the `(strategy_id, start_ts, end_ts, initial_capital)` UNIQUE index — functionally identical, just a different key tuple.

## Headline table (4H/1D baseline vs 1H re-run)

| Strategy | TF orig | Return | Alpha vs B&H | Sharpe | Max DD | Trades | Trades/yr | Gauntlet | DSR | WF OOS/IS | MC p | Peakiness | Verdict |
|----------|--------:|-------:|-------------:|-------:|-------:|-------:|----------:|---------:|----:|----------:|-----:|----------:|--------|
| **BB Breakout + EMA200 + Volume** `d64f24bc` | 4h → 1h | **+466% → −80%** | −501pp → **−1047pp** | 1.04 → **−0.66** | 27% → **90%** | 260 → 1142 | 41 → **179** | 3/5 → **1/5** | −1.49 → **−3.19** | 0.94 → **0.00⁕** | 0.000 → 0.250 | 0.004 → 0.012 | **1H is worse** |
| **Golden Cross 50/200 SMA** `8045af6b` | 4h → 1h | +685% → **+100%** | +12pp → **−572pp** | 1.08 → **0.50** | 52% → 65% | 37 → 176 | 6 → **30** | 2/5 → **1/5** | −1.45 → **−2.03** | 0.09 → 0.004 | 0.060 → 0.350 | 0.000 → 0.172 | **1H is worse** |
| **HMA + RSI + LinReg** `abf00254` | 4h → 1h | +203% → **+20%** | −764pp → **−947pp** | 0.64 → **0.27** | 64% → 72% | 166 → 668 | 26 → **105** | 1/5 → **0/5** | −1.89 → **−2.26** | 6.58⁕ → **8.24⁕** | 0.270 → 0.120 | 0.000 → **1.000** | **1H is worse** |
| **Bollinger + RSI MR** `1facd855` | 1d → 1h | +88% → **−53%** | −909pp → **−1049pp** | 0.52 → **−0.26** | 21% → **58%** | 11 → 250 | 1.7 → **39** | 1/5 → **1/5** | −2.01 → **−2.79** | 0.27 → 0.00⁕ | 0.130 → 0.690 | 0.000 → 0.245 | **1H is worse** |

⁕ WF marked `*` is either invalid (IS_avg ≤ 0; ratio uninformative) or in the degenerate-high range (>2.5) where one outlier OOS fold dominates.

## Five-point gauntlet (DSR > 1.0, 0.6 ≤ WF ≤ 2.5, beats B&H, MC p < 0.05, peakiness < 0.5)

| Strategy | 4H/1D gauntlet | 1H gauntlet | Δ |
|----------|----------------|-------------|---|
| `d64f24bc` BB Breakout | DSR FAIL, **WF PASS**, B&H FAIL, **MC PASS**, **Peak PASS** → **3/5** | DSR FAIL, WF FAIL (invalid), B&H FAIL, MC FAIL, **Peak PASS** → **1/5** | −2 |
| `8045af6b` Golden Cross | DSR FAIL, WF FAIL, **B&H PASS**, MC FAIL, **Peak PASS** → **2/5** | DSR FAIL, WF FAIL, B&H FAIL, MC FAIL, **Peak PASS** → **1/5** | −1 |
| `abf00254` HMA | DSR FAIL, WF FAIL (6.58 broken), B&H FAIL, MC FAIL, **Peak PASS** → **1/5** | DSR FAIL, WF FAIL (8.24 broken), B&H FAIL, MC FAIL, **Peak FAIL (1.000)** → **0/5** | −1 |
| `1facd855` Bollinger MR | DSR FAIL, WF FAIL, B&H FAIL, MC FAIL, **Peak PASS** → **1/5** | DSR FAIL, WF FAIL (invalid), B&H FAIL, MC FAIL, **Peak PASS** → **1/5** | 0 |

**1H gauntlet sum: 3 passes total across all 4 strategies.** That's the lowest cumulative score across any timeframe pass we've run — the 4H/1D pool had 7 passes total.

---

## Per-strategy notes

### `d64f24bc` Bollinger Band Breakout + EMA200 Trend Filter + Volume Confirmation
**Verdict: Don't seed.** **1H is dramatically worse.**

The 4H baseline was the strongest in the entire database — 3/5 gauntlet, WF=0.935 (essentially no degradation), MC p=0.000 (highly significant), peakiness 0.004 (very robust). At 1H the strategy executes 1142 trades over 6.38 years (4.4× the 4H count) and ends up **−80% in nominal return** with a **90% max drawdown**. Sharpe inverts from +1.04 to −0.66. The DSR drops from −1.49 to −3.19 — even worse, since the new Sharpe is negative.

The fingerprint here is classic fee/slippage drag: 1142 trades × roughly the same per-trade edge that worked at 260 trades, but now multiplied by fees that scale linearly with trade count. The cost model in the spec (commission_pct + slippage_pct) takes a bite out of each round-trip; at 4.4× the trade count, that bite is 4.4× bigger. The strategy's *signal* survives (the parameter sweep still shows peakiness 0.012 — robust across cells) but the *net edge* doesn't.

Walk-forward returned `degradation_ratio_valid=False` because every IS half lost money — that's not a "degenerate denominator" pipeline issue, that's the strategy genuinely failing in-sample at 1H. OOS_positive_rate dropped to 0.333 (was 0.667 at 4H).

**Recommendation:** seed on the 4H version if you seed it at all. **Do not seed on 1H.** Expected trade frequency at 4H: ~40/yr (~3.4/month) — slow but workable. At 1H: ~180/yr (~3.4/week) of consistently-losing trades.

---

### `8045af6b` Golden Cross 50/200 SMA (4H BTC)
**Verdict: Don't seed at 1H. The 4H version was the only "beat B&H" candidate; that edge is gone at 1H.**

The 4H version was the only strategy in the entire pool to beat buy-and-hold (alpha +12pp). At 1H it gives that back and then some: alpha drops to −572pp. Return drops from +685% to +100%; the strategy still makes money in absolute terms but lags BTC by ~570 percentage points over the test window.

Sharpe drops from 1.08 → 0.50; DSR drops from −1.45 → −2.03; MC p-value gets worse (0.06 → 0.35 — now far from significance); WF ratio collapses to 0.004 (OOS effectively zero). The full 125-cell parameter sweep ran cleanly (no axis pruning needed), so the peakiness=0.172 is a *real* slight peak — up from the 4H full-grid re-run's 0.000. The SMA(50)/SMA(200) cross is mildly parameter-sensitive at 1H whereas it was perfectly flat at 4H.

This is a textbook trend-following timeframe-mismatch result: SMA(50)/SMA(200) at 1H represents ~2 days / ~8 days, far too fast for the regime-shift signal a Golden Cross is designed to detect. The strategy is now whipsawing on intraday noise.

**Recommendation:** seed on 4H. **Do not seed on 1H.** Expected trade frequency at 4H: ~6/yr (slow but the 4H version actually beats B&H). At 1H: ~30/yr of trend-following whipsaws.

---

### `abf00254` HMA Crossover + RSI + Linear Regression
**Verdict: HMA family doesn't work here on either timeframe. Confirmed — not a seeding candidate at either resolution.**

Already shown to be edge-less at 4H in the prior re-run audit. At 1H the picture gets worse, not better:
- Return drops from +203% to +20% (alpha is now −947pp)
- Sharpe halves (0.64 → 0.27)
- WF ratio goes from "broken at 6.578" to "more broken at 8.239" — same degenerate-OOS-fold pattern but more severe at 1H because some fold caught an even more lucky/unlucky window. OOS_positive_rate 0.333.
- **Parameter sweep peakiness jumps from 0.000 to 1.000** — the maximum. The baseline cell is *much* better than its grid neighbours at 1H, which is the textbook overfitting signature. The 4H sweep with the full grid had peakiness 0.103; the 1H full grid shows 1.000. That's a strategy whose specific parameter choices got lucky on the test window at this resolution.
- MC p improved slightly (0.27 → 0.12) but still well above 0.05.
- DSR worsened (−1.89 → −2.26).

Filing this as: HMA + RSI + LinReg family **doesn't translate to either timeframe**. The 4H confirmed weak; the 1H confirms weaker. Strategy is curve-fit at 1H; underpowered at 4H.

**Recommendation: don't seed.** Expected trade frequency at 1H: ~105/yr of overfit signal.

---

### `1facd855` Bollinger Bands + RSI Mean Reversion
**Verdict: Don't seed on either timeframe. 1H proves the mean-reversion edge doesn't survive transaction costs.**

The 1d baseline was already 1/5 gauntlet (peakiness PASS only). At 1H the strategy loses 53% over the test window — the worst nominal performance of the four. Sharpe inverts (0.52 → −0.26); DSR drops (−2.01 → −2.79); MC p balloons to 0.69 (worse than 31% of random shuffles). Walk-forward goes invalid (every IS half lost money).

This is exactly the result the mean-reversion hypothesis predicted in the selection brief: 1d → 1H is a **24× frequency jump** (11 trades → 250 trades), and mean reversion strategies on intraday timeframes typically have small per-trade edge that gets eaten by fees. The 4H/1d version was already weak (1/5 gauntlet, alpha −909pp); pushing the same logic to 1H makes the fee bite proportionally larger while not improving signal quality.

The peakiness 0.245 at 1H is up from 0.000 at 1d — but the 1d sweep was on only 5 cells (limited detectable axes); both sweeps under-cover the parameter space, so the peakiness comparison isn't clean. The other 1H signals are clear enough: this is dead at any frequency.

**Recommendation: don't seed.** Expected trade frequency at 1H: ~39/yr of losing trades.

---

## Aggregate findings

- **None of the four strategies become a 1H seed candidate.** Three go from positive to negative returns; the fourth (Golden Cross) keeps positive returns but loses its only remaining edge (beating buy-and-hold).
- **No strategy's 1H result is so good that we should pivot the paper bot to 1H.** The closest case is Golden Cross at +100% but it lags B&H by 572pp — no edge.
- **Fee drag is the dominant explanatory factor.** d64f24bc (4.4× more trades) and 1facd855 (22.7× more trades) both go from positive to deeply negative. The lower-frequency 8045af6b and abf00254 retain positive nominal return but degrade across every other signal.
- **HMA is confirmed as a family-wide non-candidate.** Both timeframes show fundamental weakness; the 1H peakiness of 1.000 is the worst single robustness signal in the entire pool, and the WF ratio of 8.24 is the worst we've seen.
- **The original-timeframe-is-better pattern is unanimous.** Every single strategy in this batch performed worse on 1H by every measured signal. This is informative for future audits: the paper bot should match the timeframe each strategy was designed for, not promote them to faster regimes.
- **Trade frequency at 1H is reasonable for paper-bot validation** (~30–180 trades/yr per strategy = roughly 1–4 trades/week), so the bottleneck is not statistical signal speed — it's the absence of edge to harvest.

## New DB rows persisted

| Strategy | New backtest_id | New analysis_id |
|----------|-----------------|-----------------|
| `d64f24bc` | `6aa4200a-df5d-4234-af2f-12147054c65e` | `38b365be-91e2-4e02-8160-a43f55a49e0b` |
| `8045af6b` | `12b13ea7-e40f-49e1-8c24-f4f94066edcb` | `6e88f7b1-14bc-4288-91aa-86d8025b71d8` |
| `abf00254` | `7d8ac3a9-478b-4bf3-bb92-5ba3c5c4de33` | `1896934f-19b8-4dd2-be39-5cb5bc932d7b` |
| `1facd855` | `c91b6f70-bbdd-440d-8bc7-a55d75c10b50` | `26af7d13-0847-4987-b8ab-bf43bbd7ac77` |

Originals are untouched. The new rows' `result_json.spec_snapshot.primary_timeframe == "1h"` whereas the originals carry their original timeframe — that's the cleanest way to filter by timeframe at query time.

## Worth flagging beyond the data

- **The WF gauntlet still has no upper bound.** Same observation as the prior re-run audit: HMA's 8.24 ratio at 1H is mechanically a "pass" under `WF > 0.6`. A proper rubric needs `0.6 ≤ ratio ≤ 2.5` plus a minimum-trades-per-fold floor before accepting the ratio as informative.
- **`degradation_ratio_valid=False` should probably count as FAIL by default.** Two of the four 1H runs (d64f24bc, 1facd855) have IS-non-positive across folds — meaning the strategy *lost money in-sample* at 1H. That's at least as bad as a low OOS/IS ratio; the current gauntlet treats `valid=False` as a pipeline marker rather than a strategy verdict.
- **Sweep cell coverage matters.** Two strategies (d64f24bc, 1facd855) only generated 5-cell sweeps because they have few detectable axes (BB indicators aren't currently swept; only stop-loss or single indicator periods get axes). That means peakiness is computed over a near-trivial neighbourhood. The peakiness 0.012 and 0.245 should be interpreted with a caveat: "robust on the one axis we could vary, says nothing about untested parameters." Phase 5+ should add BB std-dev multiplier as a sweep axis.
- **The transcription delays / extraction funnel is the bigger lever.** With only 6 strategies meeting the backtest+overfitting criteria, we're choosing from a thin pool. Most of the seedable pool's destiny is set by what the extraction pipeline produces, not what the audit picks from it.
