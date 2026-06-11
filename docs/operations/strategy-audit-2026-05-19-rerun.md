# Strategy audit re-run — 2026-05-19

> **Golden Cross verdict: weak. HMA verdict: weak.**
> Both strategies' five-point gauntlets stay in "Don't seed" territory after re-running with corrected parameters; neither becomes a paper-bot candidate. The re-run did, however, replace bogus signals with legitimate ones — the originals contained pipeline artefacts that masked the underlying weakness.

Companion to `strategy-audit-2026-05-19.md`. Targets the two pipeline failures flagged in that report:

- **Golden Cross (`8045af6b`)** — parameter sweep was 25 cells instead of the full 125, because the original code's 50-cell cap forced one axis to be dropped. We now have full-grid robustness data.
- **HMA (`abf00254`)** — walk-forward returned `OOS/IS = 6.578`, which mechanically counted as "pass" in the original gauntlet despite being a degenerate ratio dominated by one bull-run OOS fold. We re-ran with `n_windows=3` so each fold has enough trades for a stable denominator.

Raw analysis sidecars: `docs/operations/strategy-audit-2026-05-19-rerun-data/{golden_cross,hma}_rerun.json`.

The two new `OverfittingAnalysis` payloads were **not** persisted to the database because `overfitting_analyses` has a `UNIQUE` index on `backtest_id` and the originals already occupy that slot. The originals are the historical record; this report + the JSON sidecars are the corrected analysis.

---

## Parameter sweep cap (Section 1 of the original brief's task list)

- The cap is `_MAX_CELLS = 50`, a module-level constant in `workers/src/marketmind_workers/overfitting/parameter_sweep.py:66`. There is no env var; the worker job calls `run_parameter_sweep(spec, start, end, data_dir=data_dir)` with the default cap (`workers/src/marketmind_workers/jobs/overfitting_analysis.py:105`). The public function does accept a `max_cells=` kwarg, which is what these re-runs used — no source edit, no env var, just a direct call from a one-off script.
- Pruning is impact-ordered: when the cartesian product exceeds the cap, axes are dropped in order of lowest impact rank (`_AXIS_IMPACT_RANK`), with `INDICATOR_PERIOD` axes broken by ascending `baseline_value` — i.e. the slowest indicator is dropped first when there are multiple indicator-period axes.
- Both Golden Cross and HMA had three axes detected (stop-loss + two indicator periods for GC, three indicator periods for HMA), giving a 5×5×5 = 125-cell grid. Under the 50-cap, the slow-MA axis was dropped in both cases, collapsing to 25 cells.
- Lifting the cap to **200** for the re-run fits the full 125 cells with headroom; sweep step took roughly an extra 25-30 seconds per strategy (full-pipeline elapsed: 30.4s GC, 42.7s HMA).

---

## Walk-forward degenerate ratio (Section 3 of the brief)

The HMA original walk-forward ran with the default `n_windows=6, train_ratio=0.7` over Jan 2020 → May 2026 (~6.4 years). Each window was ~12.8 months; IS half ~9 months, OOS half ~3.8 months. Per-fold trade counts:

| window | IS trades | IS return | OOS trades | OOS return |
|--------|----------:|----------:|-----------:|-----------:|
| 0 | 20 | +0.105 | 4 | **+1.229** |
| 1 | 20 | +0.171 | 7 | -0.154 |
| 2 | 20 | -0.384 | 5 | +0.315 |
| 3 | 20 | +0.227 | 5 | +0.514 |
| 4 | 17 | +0.129 | 10 | -0.129 |
| 5 | 18 | -0.005 | 9 | -0.177 |

Two factors compound:

1. **OOS half too short** — at 4-10 trades per OOS fold the per-fold return is dominated by individual outcomes; statistical noise overwhelms any signal.
2. **Window 0 captured the late-2020 BTC rally** — a 4-trade OOS half produced a +122.9% return on a strategy whose IS half over the *same* period managed +10.5%. With IS_avg = 0.040 and OOS_avg = 0.266, the ratio mechanically computes to 6.578.

The original audit's gauntlet rule (`OOS/IS > 0.6 = PASS`) had no upper bound, so 6.578 mechanically passed. It shouldn't — a healthy ratio sits near 1.0; ratios above ~2.0 are almost always artefacts (degenerate denominator, single-window blowout, or both). Worth a Phase 5+ gauntlet revision but out of scope here.

**Corrective parameter:** reduce `n_windows` from 6 to 3. Each window becomes ~25.6 months (IS ~17.9 months, OOS ~7.7 months); per-fold trade volumes triple. See the HMA re-run table below.

---

## Golden Cross (`8045af6b`) — original vs re-run

| Signal | Original | Re-run | Δ |
|--------|---------:|-------:|---|
| Walk-forward OOS/IS | **0.093** (FAIL) | **0.093** (FAIL) | unchanged — WF was not re-parameterised |
| Walk-forward `degradation_ratio_valid` | true | true | — |
| Parameter sweep cells | 25 (1 axis dropped) | **125** (full grid) | +100 cells |
| Sweep peakiness | 0.000 | **0.000** | unchanged — robust |
| Sweep baseline rank percentile | 0.560 | **0.592** | +0.03 (more data, similar position) |
| Sweep best return | 13.79% | 18.45% | new best in dropped axis neighbourhood |
| Sweep worst return | 4.43% | 1.28% | new worst, but still positive |
| Sweep neighborhood avg | 7.95% | 7.71% | similar |
| Sweep skipped_reason | `dropped: Indicator period (200)` | `null` | full grid |
| Monte Carlo p-value | 0.060 | 0.060 | unchanged (same data, same seed) |
| Monte Carlo percentile | 0.940 | 0.940 | unchanged |
| Deflated Sharpe ratio | -1.453 | -1.453 | unchanged (closed-form on baseline) |
| Composite score | 47.19 | **47.19** | unchanged |
| Composite verdict | `mixed_signals` | `mixed_signals` | unchanged |

**Sweep axes that now ran (vs original 2 axes):**
- Stop-loss %: [0.04, 0.06, 0.08, 0.10, 0.14] baseline 0.08
- Indicator period (50): [25, 38, 50, 62, 88] baseline 50
- Indicator period (200): [100, 150, 200, 250, 350] baseline 200 ← was dropped originally

### Updated five-point gauntlet — Golden Cross

| Check | Original | Re-run |
|-------|----------|--------|
| Deflated Sharpe > 1.0 | FAIL (-1.453) | **FAIL (-1.453)** |
| Walk-forward OOS/IS > 0.6 | FAIL (0.093) | **FAIL (0.093)** |
| Beats buy-and-hold | PASS (+12.03%) | **PASS (+12.03%)** |
| Monte Carlo p < 0.05 | FAIL (0.060) | **FAIL (0.060)** |
| Parameter sweep robust | BORDERLINE (skipped) | **PASS** (peakiness 0.000, baseline rank 59th pct, full 125-cell grid) |

**Verdict for seeding:** Don't seed (3 fails / 2 passes).

**What changed:** the borderline "skipped" sweep flipped to a genuine PASS — peakiness is zero across all 125 cells, including the previously-untested slow-SMA neighborhood (100, 150, 200, 250, 350). The strategy is genuinely parameter-robust. But that doesn't move the needle: DSR is still deeply negative, walk-forward OOS is still ~zero, and Monte Carlo is still p=0.06 (just outside significance). The strategy is robust *to its own parameters*, but the underlying edge isn't statistically distinguishable from luck. Beats buy-and-hold over the test window, but only by 12pp over ~6 years — well within Monte Carlo noise.

---

## HMA (`abf00254`) — original vs re-run

| Signal | Original | Re-run | Δ |
|--------|---------:|-------:|---|
| Walk-forward `n_windows_actual` | 6 | **3** | -3 |
| Walk-forward IS avg return | +0.040 | **+0.540** | +0.50 (longer IS = more trades, more compounding) |
| Walk-forward OOS avg return | +0.266 | **+0.224** | similar |
| Walk-forward OOS/IS ratio | **6.578** ← broken | **0.416** ← legitimate | -6.16; finally interpretable |
| Walk-forward `degradation_ratio_valid` | true | true | — |
| Walk-forward OOS positive rate | 0.500 | 0.667 (2 of 3 OOS halves positive) | +0.17 |
| Walk-forward consistency score | 0.475 | 0.473 | unchanged |
| Parameter sweep cells | 25 (1 axis dropped) | **125** (full grid) | +100 cells |
| Sweep peakiness | 0.000 | **0.103** | +0.10 (slight peak emerges with full grid) |
| Sweep baseline rank percentile | 0.520 | 0.520 | unchanged |
| Sweep skipped_reason | `dropped: Indicator period (64)` | `null` | full grid |
| Monte Carlo p-value | 0.270 | 0.270 | unchanged |
| Deflated Sharpe ratio | -1.894 | -1.894 | unchanged |
| Composite score | 28.59 | **52.49** | +23.9 |
| Composite verdict | `likely_robust` ← bogus | `mixed_signals` | corrected |

**Walk-forward re-run windows (n=3):**

| window | IS trades | IS return | OOS trades | OOS return |
|--------|----------:|----------:|-----------:|-----------:|
| 0 | 37 | +1.716 | 18 | +0.059 |
| 1 | 39 | -0.168 | 12 | +0.924 |
| 2 | 37 | +0.071 | 17 | -0.309 |

Trade volume per fold is now in a usable range (12-39 trades), and IS_avg = 0.540 is large enough that the ratio (0.416) is no longer noise-dominated.

**Sweep axes that now ran (vs original 2 axes):**
- Indicator period (14): [7, 10, 14, 18, 24] baseline 14
- Indicator period (16): [8, 12, 16, 20, 28] baseline 16
- Indicator period (64): [32, 48, 64, 80, 112] baseline 64 ← was dropped originally

### Updated five-point gauntlet — HMA

| Check | Original | Re-run |
|-------|----------|--------|
| Deflated Sharpe > 1.0 | FAIL (-1.894) | **FAIL (-1.894)** |
| Walk-forward OOS/IS > 0.6 | "PASS" (6.578) ← bogus | **FAIL (0.416)** ← legitimate |
| Beats buy-and-hold | FAIL (alpha -763.89%) | **FAIL (alpha -763.89%)** |
| Monte Carlo p < 0.05 | FAIL (0.270) | **FAIL (0.270)** |
| Parameter sweep robust | BORDERLINE (skipped) | **PASS** (peakiness 0.103, baseline rank 52nd pct, full 125-cell grid) |

**Verdict for seeding:** Don't seed (4 fails / 1 pass).

**What changed:**
- The walk-forward "PASS" of 6.578 was always a pipeline artefact, not a real signal. The legitimate 0.416 ratio is honest: OOS returns degrade to ~42% of IS — that's *mild degradation* territory per the walk-forward docstring, not a fatal signal but well below the 0.6 threshold for a passing gauntlet.
- The sweep flipped from borderline-skipped to PASS — adding the slow-MA axis (period 64) didn't break robustness. Peakiness ticked from 0.000 to 0.103, which is still well below the 0.5 "sharp peak" threshold.
- Composite score went UP (28.59 → 52.49) and verdict went from `likely_robust` to `mixed_signals` — the composite scorer was rewarding the broken 6.578 ratio as if it were a great OOS result. The new score is a more honest reflection.
- Gauntlet fail count went from 3 → 4 because the bogus WF pass is now a legitimate WF fail. HMA is *clearly* not edge-bearing — strategy doesn't beat buy-and-hold, doesn't survive permutation testing, doesn't have a deflated Sharpe edge, and degrades materially out-of-sample. Robust *parameters*, no actual *edge*.

---

## Summary

| Strategy | Original verdict | Re-run verdict | Rationale |
|----------|------------------|----------------|-----------|
| Golden Cross | Don't seed (3F/1P/1B) | **Don't seed** (3F/2P) | Sweep robustness confirmed full-grid; everything else unchanged |
| HMA | Don't seed (3F/1P/1B; WF pass was bogus) | **Don't seed** (4F/1P) | WF re-run revealed legitimate failure; composite verdict honestly reclassified to `mixed_signals` |

Neither strategy clears the bar for paper-bot seeding. The re-run did its job — it replaced two pipeline artefacts with legitimate measurements — but the corrected measurements are no kinder to either strategy.

**No new candidates emerge.** Recommend continuing the extraction pipeline (the YouTube content funnel has produced 13 refusals to 6 viable specs; broadening source variety may help) rather than further analysis of the existing pool.

### Worth flagging beyond the data

- **Gauntlet rule missing upper bound on walk-forward ratio.** `OOS/IS > 0.6 = PASS` mechanically accepts 6.578 as a pass when it should be a flag. A future audit rubric should bound the ratio (e.g., `0.6 ≤ ratio ≤ 2.5`), and additionally require a minimum trade count per fold (perhaps 10 IS + 10 OOS) before accepting the ratio as informative at all.
- **Sweep cap behaviour is invisible to the gauntlet.** A skipped axis silently shows up as `peakiness_score=0` because there's no neighbour on the dropped axis to compare against — that's *not* the same as "robust". The audit script's original BORDERLINE label was correct; the composite scorer's `parameter_sweep_contribution` should probably penalise (or flag) sweeps with `skipped_reason != None`.
- **Composite verdict drift on the HMA re-run** (likely_robust → mixed_signals despite no underlying strategy change) is a sign the composite scoring formula is sensitive to the walk-forward ratio in ways that aren't intuitive. Worth a future pass.
- **Anthropic prompt cache stayed warm across re-runs** — no LLM calls were involved (these are pure backtest replays); only mentioned for completeness.
