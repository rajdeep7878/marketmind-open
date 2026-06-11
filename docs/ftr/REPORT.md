# FTR (Frequent-Trading Research) — Build & Validation Report

Branch `v2-phase-d-ftr` · built 2026-06-11 · all numbers **net of the named
cost profile**, past tense, no forward-looking claims.

## Bottom line

**No strategy is deploy-eligible for paper trading.** Every (strategy ×
venue profile) cell of the verdict matrix is REJECTED or INSUFFICIENT_DATA.
This is the system working: the validation gauntlet exists to stop
plausible-looking configurations from reaching even a paper book.

The infrastructure — data layer, feature pipelines, two parity-checked
engines, G1–G9 gauntlet, paper-only trader, recorder, API/CLI — is complete,
tested, and preserved for the next experiment round.

## Verdict matrix (strategy × venue profile)

| strategy | profile | verdict | net ret | gross | Sharpe | maxDD | trades/day | hold | cost/edge | failed gates |
|---|---|---|---|---|---|---|---|---|---|---|
| ml_hourly_btc_longflat | binance_spot_reference (26 bps RT) | **REJECTED** | +153.6% | +2005% | 0.54 | −53% | 0.289 | 12 h | 0.62 | G2,G3,G4,G6,G7 |
| ml_hourly_btc_longflat | kraken_pro_uk_tier0 (90 bps RT) | **REJECTED** | +5.0% | +435% | 0.14 | −34% | 0.064 | 13 h | 0.90 | G1,G2,G3,G4,G6,G7,G8 |
| ml_hourly_btc_longflat | coinbase_advanced_uk_tier0 (130 bps RT) | **REJECTED** | +17.4% | +64.6% | 0.31 | −14% | 0.009 | 7 h | 0.66 | G2,G3,G4,G6,G7 |
| trend_4h_portfolio | binance_spot_reference | **REJECTED** | +112.6% | +134.7% | 1.11 | −28% | 0.149 | 239 h | 0.07 | G3,G4 |
| trend_4h_portfolio | kraken_pro_uk_tier0 | **REJECTED** | +69.7% | +134.7% | 0.82 | −33% | 0.149 | 239 h | 0.24 | G3,G4 |
| trend_4h_portfolio | coinbase_advanced_uk_tier0 | **REJECTED** | +47.7% | +134.7% | 0.65 | −37% | 0.149 | 239 h | 0.34 | G2,G3,G4 |
| ofi_microstructure_research | all | **INSUFFICIENT_DATA** | — | — | — | — | — | — | — | recorder sample 0 d / 28 d required |

ML: stitched OOS 2018-09 → 2026-06 (94 purged walk-forward folds, embargo
24 h, n_trials = 36 honestly counted across family × horizon × p_min).
Trend: selection 2017 → 2024-01 (144-cell grid), holdout 2024-01 → 2026-06,
n_trials = 144.

### Baselines alongside (same windows, same profiles)

| baseline | window | net |
|---|---|---|
| BTC buy & hold (ML window, kraken) | 2018-09→2026-06 | +841% |
| **naive sign, no EV gate (ML, kraken)** | same | **−100%** (3,189 trades — cost ruin) |
| matched-frequency random entry (ML) | same | real beat 100.0% of 1,000 sims |
| BTC buy & hold (trend holdout, kraken) | 2024-01→2026-06 | +46.8% |
| equal-weight 12-coin universe (kraken) | same | −31.3% |
| Modern-Turtle-class proxy, BTC only (kraken) | same | +9.9% |
| circular-shift timing null (trend) | same | real beat 99.6% of 1,000 sims |

## What the evidence says

1. **Costs are the binding constraint, exactly as the priors stated.** The
   same ML decisions gross +435% on Kraken economics but net +5% — 164% of
   starting equity went to fees/spread/slippage (cost/edge 0.90). The EV
   gate compressed frequency from a naive ~1.2 trades/day to 0.064/day on
   Kraken and still couldn't clear the 100 bps hurdle (90 RT + 10 safety).
2. **The predictive signal is real but too small to sell at retail costs.**
   Label-permutation refit (20 block-permuted refits, every-8th-fold
   subset): real OOS AUC 0.5424 > permuted 95th pct 0.5217. The matched-
   frequency random-entry null was beaten by 100% of sims. None of that
   survives G2/G3 after costs — real ≠ monetizable.
3. **The EV gate prevented ruin.** Without it (naive sign baseline) the
   identical model loses 96–100% on every profile. The gate is the
   strategy; the model is an input.
4. **Trend is the closest miss, on the most accessible venue.** Kraken
   holdout: Sharpe 0.82 (passes G2), plateau 0.95 (passes G7), cost/edge
   0.24 (passes G6), beats every baseline, 99.6th pct vs the timing null —
   and still fails G3 (DSR with 144 trials on a 2.4-year holdout) and G4
   (<60% of 30-day windows positive; trend P&L concentrates in bursts).
   Honest verdict: REJECTED. A longer holdout is the only legitimate cure —
   not a looser gate.
5. **Pseudo-diversification is measured, not assumed**: average pairwise
   correlation of held assets 0.717 → effective breadth N_eff = 1.25 from
   ~3.4 names held. The "portfolio" is ~1.25 independent bets.
6. **Regime concentration**: all trend profit occurred with BTC above its
   200d MA (+99.9% vs −15.1% below, Kraken). The optional regime gate was
   swept (on/off) and selection chose OFF — the gate's costs exceeded its
   savings in-sample.
7. **Frequency**: honest cost-gated calibration yields 0.01–0.29 trades/day
   — far below the 1–5/day aspiration. Per the mandate, that IS the answer:
   the 1–5/day band is not reachable at UK-retail costs with these signal
   families without paying ruinous cost drag.
8. **OFI**: the recorder must collect ≥28 days at ≥95% uptime before any
   verdict; that sample does not exist yet (INSUFFICIENT_DATA). Expected
   outcome per the literature once sampled: REJECTED in taker mode.

## Overlay ablation (1m-data window, trailing 180 d only)

| profile | net without overlay | net with overlay | entries deferred/skipped |
|---|---|---|---|
| binance_spot_reference | −6.25% | −0.83% | 25 |
| coinbase_advanced_uk_tier0 | +7.41% | 0.00% | 6 |
| kraken_pro_uk_tier0 | 0.00% (no entries in window) | 0.00% | 0 |

Mixed, small-sample evidence: the overlay clipped losses on one profile and
clipped a small gain on another. It remains an execution cost-reducer with
a mandatory ablation, not alpha — no verdict relies on it.

## Engine integrity evidence

- Drift parity (vector vs Decimal event engine) on every chosen cell:
  identical trade timestamps, net-return relative diff 1.0e-5 – 5.7e-5
  (lot-quantization scale). CI gate: `test_ftr_drift_parity`.
- Determinism: byte-identical decision log + equity hash across runs,
  pinned by golden file (`test_ftr_determinism_golden`).
- 24h-block bootstrap CIs and full sweep-cell tables persisted under
  `data/ftr/artifacts/validation/<family>/<runstamp>/`.

## Paper-only safety evidence

| guard | proof |
|---|---|
| ExecutionMode has exactly one member (PAPER); no env/config escape | `test_ftr_paper_only.py::test_execution_mode_has_exactly_one_member`, `::test_no_live_env_escape`, `::test_ftr_settings_has_no_execution_mode_field` |
| Trader's first executable statement is the paper assert | module-level assert in `ftr/trader/runner.py` before all imports |
| PublicOnlyExchange: keyless, whitelist-only, raises PaperOnlyViolation | `::test_public_only_exchange_blocks_private_methods` |
| No API-key plumbing anywhere in FTR | `::test_no_api_key_plumbing_in_ftr_modules` (source scan) |
| UK retail compliance: derivatives rejected by type for ANY mode | `test_ftr_paper_only.py::test_uk_compliance_rejects_derivative_instruments`, `::test_instrument_type_is_spot_only_by_type` |
| research_simulation_only frozen True on OFI; trader refuses BY TYPE | `::test_research_simulation_only_is_frozen_true_on_ofi`, `::test_paper_trader_refuses_research_only_by_type` |
| Deployment requires PASS verdict on uk-feasible venue | `load_deployments` gate + `::test_paper_trader_refuses_infeasible_venue` |
| Risk guards: daily −2%, maxDD −10% (sticky), 8 trades/day, cooldown, kill switch (DB flag or KILLSWITCH file) | `test_ftr_guardrails_trip.py` (5 tests; also caught and fixed a guard-ordering bug) |
| Decimal ledger + lot quantization + caps | `test_ftr_sizing_caps_decimal.py` |
| Crash safety: idempotent decision key + state recovery | `test_ftr_crash_recovery.py` (unit + integration-marked DB round trip) |

## Known limitations

1. **Recorded-L2 sample = 0 days.** All microstructure verdicts are
   INSUFFICIENT_DATA until the recorder runs ≥28 days at ≥95% uptime.
   Recorder fixtures are labeled `FIXTURE — NOT MARKET DATA` and feed unit
   tests only.
2. **Venue fee tiers are June-2026 base-tier snapshots** (Kraken 25/40,
   Coinbase 40/60 — some sources report Coinbase 40/60 inverted), encoded
   as config marked "verify at runtime". Maker-fee economics were not used
   for verdicts (taker/taker pessimism).
3. **Trend holdout covers 2.4 years (2024-01→2026-06)** — one regime cycle.
   G3/G4 failures are partly a sample-size statement; the cure is more
   holdout data, not looser gates.
4. **Effective breadth ≈ 1.25** — the trend "portfolio" is not meaningfully
   diversified, and its P&L is concentrated in BTC-above-200dMA regimes.
5. **Data QA flags**: 1m series carry rolling-MAD outlier flags (25–308 per
   symbol — flagged, never deleted). Kraken's public OHLC API serves only
   ~720 recent candles, so the cross-venue sanity series covers ~30 days,
   not full history. The known repo-level 15m gap does not affect FTR
   (FTR uses its own 1m/1h/4h caches, QA-verified gap-free).
6. **Universe survivorship**: point-in-time monthly re-selection uses only
   data available at selection time, but coins delisted from Binance before
   2026 are invisible to today's API. The 12-coin superset is survivorship-
   tinted in their favor; verdicts were REJECTED anyway.
7. **Postgres was down during the build** (Docker Desktop integration off
   in this WSL session): verdicts persist as JSON artifacts;
   `insert_verdict_rows` + migration 0020 are wired and integration-tested
   via testcontainers when Docker is available.
8. **ML expectancy proxy for the portfolio path**: the trend gauntlet uses
   entry-count expectancy and a monthly-PF proxy (no per-trade ledger on
   the weights path) — stated in every gate report's notes.

## Next experiments (ranked)

1. **Extend the trend holdout / walk the selection forward quarterly.**
   Hypothesis: the chosen cell (EMA 50/200, Donchian-80, 4×ATR chandelier,
   4h) sustains Sharpe ≥ 0.8 net on Kraken with DSR ≥ 0.95 once the
   evaluation window reaches ~4–5 years. Gate to beat: G3+G4 on
   kraken_pro_uk_tier0.
2. **Maker-side execution for the trend entries** (post-only at the touch,
   pessimistic-fill model already built in 3.3). Hypothesis: Kraken maker
   25 bps vs taker 40 bps cuts round-trip ~30 bps and lifts net Sharpe
   ≥ 0.1; must NOT use queue-position optimism. Gate: G2+G3 on Kraken.
3. **Run the recorder for 28+ days, then execute the OFI measurement
   pipeline.** Hypothesis (from the literature, stated in the module
   docstring): predicted moves are sub-spread ⇒ taker verdict REJECTED;
   the deliverable is the calibrated spread/depth/adverse-selection inputs
   for the liquidity overlay. Gate: the INSUFFICIENT_DATA sample gate.
4. **Lengthen ML horizons (48–96 bars) on UK-tier costs.** Hypothesis: the
   signal's per-trade edge grows with horizon faster than vol drag;
   frequency falls further (PASS_LOW_FREQUENCY territory) but G1/G6 may
   clear. Gate: G1+G6+G8 on kraken_pro_uk_tier0.
5. **Overlay validation at scale**: once 6+ months of 1m + recorded spread
   exist, re-run the with/without ablation; adopt only if the cost saving
   is positive with a bootstrap CI excluding zero.

## Reproduction commands

```bash
# data (resumable; ~100 MB parquet)
.venv/bin/python -m marketmind_workers.ftr.data.fetch_all

# recorder (opt-in; runs until stopped; hourly rotation)
.venv/bin/python -m marketmind_workers.ftr.data.recorder
# or: docker compose --profile ftr up -d ftr-recorder

# full validation gauntlet
.venv/bin/python -m marketmind_workers.ftr.validation.runner --strategy ml
.venv/bin/python -m marketmind_workers.ftr.validation.runner --strategy trend
.venv/bin/python -m marketmind_workers.ftr.validation.runner --strategy ofi

# reports
.venv/bin/python -m marketmind_workers.ftr.report verdicts
.venv/bin/python -m marketmind_workers.ftr.report cost-edge
.venv/bin/python -m marketmind_workers.ftr.report equity --limit 50      # needs DB
.venv/bin/python -m marketmind_workers.ftr.report decisions --limit 50   # needs DB

# paper trader (idles honestly with no PASS verdicts; needs DB for persistence)
docker compose --profile ftr up -d ftr-paper-trader

# tests
.venv/bin/python -m pytest workers/tests/test_ftr_*.py -q   # FTR suite
.venv/bin/python -m pytest -q                               # full repo suite
.venv/bin/python -m pytest -m integration workers/tests/test_ftr_crash_recovery.py  # needs Docker
```
