# FTR (Frequent-Trading Research) — Integration Plan

Branch: `v2-phase-d-ftr` (cut from `v2-phase-c-multi-asset` @ `3a4540b`, verified clean).
Status: written at Stage 0 of the FTR build, then immediately acted on. This is a record,
not a review gate.

## 0. Stage-0 verification of assumed repo facts (§3 of the build mandate)

| Assumption | Verified reality | Divergence handling |
|---|---|---|
| Branch `v2-phase-c-multi-asset` with AssetClass, OandaAdapter+VCR, per-asset-class Fee/Slippage models, SessionHours, weekend-drop | Confirmed. `AssetClass` is a `Literal` union (not StrEnum) in `shared/.../strategy_spec/common.py`; OandaAdapter at `workers/.../trader/exchanges_oanda.py` with cassettes in `workers/tests/cassettes/oanda/`; session filter `backtest/session_filter.py`; trader-side `trader/session_skip.py` | None |
| Crypto ≈ 30 bps, FX ≈ 10 bps, metals ≈ 24 bps cost model | Confirmed but **stored per-side**: BTC/USDT = 10 bps fee + 5 bps slippage per side (`_DEFAULT_FEE_TABLE` / `_DEFAULT_SLIPPAGE_TABLE`); FX = 0 + 5; metals = 0 + 12. The 30/10/24 numbers are round-trip sums of two taker sides | FTR venue profiles are defined per-side and explicitly reconciled (see §3 below). No double counting: legacy model has no separate half-spread term — its "slippage" *is* spread+impact; FTR profiles split `half_spread_bps` from `slippage_bps`, so `binance_spot_reference` taker round-trip = 2×10 + 2×1 + 2×2 = 26 bps ≈ legacy 30 bps (legacy is the more pessimistic; both reported) |
| Two engines + drift parity | Confirmed: vectorbt engine (`backtest/engine.py`) + iterative (`backtest/iterative.py`) + live stepper (`iterative_live.py`); parity gates in `tests/test_backtest_control.py` and `workers/tests/test_iterative_live_drift_parity*.py` | Both existing engines are **StrategySpec-bound, single-instrument**. ML-probability replay and multi-asset Decimal portfolio accounting cannot be expressed in StrategySpec v2.0. FTR therefore adds its own sibling pair (vectorized + event-driven) under `marketmind_workers/ftr/backtest/`, following the repo's two-engine + drift-parity-gate convention. Justification recorded here per the "replacement only with written justification" rule — this is **addition**, not replacement; existing engines untouched |
| Gauntlet: walk-forward, sweep, MC, DSR, cost sanity, composite 16–20 = strong seedable | Confirmed in `workers/.../overfitting/` (composite is 0–100, LOW is good; 0–30 = "Likely Robust", historical strong seeds ≈ 16–20). DSR `deflated_sharpe()` takes `n_observations` = **T in years** post-2026-05-25 fix | FTR reuses `deflated_sharpe()` directly (same module, honest n_trials passed in). Composite scoring is not reused — FTR's G1–G9 gate set is the verdict mechanism per the mandate; FTR verdicts additionally report the legacy-style sub-results so they read consistently |
| Seven seeded strategies, Modern Turtle = slow-trend baseline | Confirmed (DB-resident, `trader_strategy_versions`; Modern Turtle = first `template='spec'`) | Modern Turtle baseline reproduced for comparison via its spec run through the existing engine on the same window |
| DSR T-in-years footgun; WeekdayFilter ISO vs DayOfWeekCondition pandas | Confirmed | FTR uses **one** convention everywhere: pandas `Monday=0..Sunday=6` (documented in `features/hourly.py`); cyclic encodings derive from it |
| Specs via `validate_spec`/`model_validate`, never positional | Confirmed (`validator.py:validate_spec`) | FTR strategy configs are new frozen pydantic models constructed exclusively via `model_validate`; a test asserts no positional construction in FTR modules |
| Secrets in `.env` (gitignored); no committed secrets | Grep of tracked files found no committed credentials | Proceed |

Other Stage-0 facts that shape the build:

- **No YAML config convention exists** (env vars + pydantic-settings + Python constant tables only;
  pyyaml is not a dependency). Venue profiles therefore live in
  `ftr/config/venue_profiles.py` as a typed Python table mirroring the mandated YAML structure
  field-for-field — the sanctioned "repo-conventional equivalent".
- Migrations: plain SQL files `infra/db/migrations/NNNN_*.sql`, applied idempotently by
  `apply_migrations()`; latest is `0018`. FTR adds `0020_ftr_research.sql`.
- Tests: uv workspace, `pytest` from repo root; `integration` and `live_api` markers are opt-in.
- Deps to add (workers package): `xgboost`, `scikit-learn` (logistic/isotonic/ridge), `websockets`
  (recorder). pandas/pyarrow/numpy/scipy/ccxt already present.
- Network: Binance + Kraken public REST verified reachable from this host.
- API: FastAPI + psycopg, routers in `api/src/marketmind_api/routes/`, registered in `main.py`.
- Docker: compose has `postgres`, `redis`, `api`, `worker`, `trader_worker`, `web`. No profiles in
  use yet; compose supports them — FTR services use `profiles: ["ftr"]` so they are opt-in.
- CLI convention: `python -m` module mains, no click/typer. FTR CLI =
  `python -m marketmind_workers.ftr.report <verdicts|equity|decisions|cost-edge>`.

## 1. Module layout (all new files unless marked MOD)

```
workers/src/marketmind_workers/ftr/
  __init__.py
  config/
    __init__.py
    venue_profiles.py        # typed venue profiles (§4 of mandate), round_trip_cost_bps()
    settings.py              # FTRSettings (pydantic-settings): paths, symbols, cadence
  data/
    __init__.py
    ohlcv.py                 # ccxt fetcher: pagination, retries, parquet cache + manifest/checksums
    quality.py               # data QA validator -> QAReport rows for ftr_data_quality
    recorder.py              # Binance spot L1/L2 async recorder (depth@100ms diff + snapshot resync)
    recorder_fixtures.py     # "FIXTURE — NOT MARKET DATA" generators, unit tests only
  features/
    __init__.py
    shifting.py              # THE anti-lookahead module: shared shift/label discipline
    hourly.py                # hourly OHLCV feature pipeline (config-driven)
    micro.py                 # microstructure features (OFI per CKS, markouts, imbalances)
    splits.py                # purged walk-forward splits with embargo
    meta.py                  # feature_meta.json + snapshot hashing
  strategies/
    __init__.py
    records.py               # DecisionRecord + ReasonCode (shared enum)
    specs.py                 # frozen pydantic FTR strategy configs + UK-compliance validator
    ml_hourly.py             # 3.1 ml_hourly_btc_longflat (XGBoost + logistic, EV gate)
    trend_portfolio.py       # 3.2 trend_4h_portfolio (point-in-time universe, vol targeting)
    ofi_research.py          # 3.3 ofi_microstructure_research (research_simulation_only=True frozen)
    liquidity_overlay.py     # 3.4 overlay (Abdi–Ranaldo estimator; ALLOW/DEFER/SKIP decorator)
  backtest/
    __init__.py
    costs.py                 # round-trip cost math from venue profiles + sensitivity multipliers
    vector_engine.py         # vectorized engine (sweeps): next-bar-open fills, profile costs
    event_engine.py          # event-driven engine (final candidates): same fill law, Decimal ledger
    portfolio.py             # Decimal accounting: cash, positions, fees, lot/tick quantization
  validation/
    __init__.py
    walkforward.py           # purged WF orchestration (ML: 365/30/30 rolled; trend: expanding)
    sweep.py                 # full grids; honest n_trials accounting per strategy family
    montecarlo.py            # block bootstrap, matched-frequency random entry, label permutation
    baselines.py             # B&H, equal-weight universe, Modern Turtle ref, naive-sign, overlay-off
    gates.py                 # G1–G9 + verdict vocabulary + machine-readable failed-gate lists
    runner.py                # full validation orchestration -> ftr_verdicts + artifacts on disk
  trader/
    __init__.py
    execution_mode.py        # ExecutionMode enum, sole member PAPER
    public_exchange.py       # PublicOnlyExchange (no keys; private methods raise PaperOnlyViolation)
    paper_broker.py          # PaperBroker — the only Broker implementation
    guards.py                # risk guards, kill switch (DB flag + KILLSWITCH file)
    persistence.py           # ftr_* table I/O, idempotency keys, crash-safe state rebuild
    runner.py                # asyncio loop; FIRST STATEMENT is the paper assert
  report.py                  # CLI: python -m marketmind_workers.ftr.report ...
api/src/marketmind_api/routes/ftr.py          # /ftr/* endpoints (read-only)
api/src/marketmind_api/main.py                # MOD: register ftr router
infra/db/migrations/0020_ftr_research.sql     # ftr_* tables
docker-compose.yml                            # MOD: ftr-paper-trader + ftr-recorder (profile "ftr")
workers/pyproject.toml                        # MOD: + xgboost, scikit-learn, websockets
.gitignore                                    # MOD: ftr data/cache/recording dirs
workers/tests/test_ftr_*.py                   # Stage-7 test set
```

## 2. Reuse vs extend vs new

**Reused as-is (imported, not modified):**
- `overfitting/deflated_sharpe.py: deflated_sharpe()` — DSR with honest `n_trials`, T in years.
- `services/market_data.py` fetch/caching *patterns* (pagination, `enableRateLimit`,
  parquet+snappy, dedupe-on-merge) — FTR's fetcher follows them but adds manifests, checksums and
  QA hooks, in its own module so the legacy cache layout is untouched.
- `trader/exchanges.py: BinanceAdapter` retry/backoff conventions (mirrored, public-only).
- Existing engine *fill law* (signal at close t → fill at open t+1; fees on entry+exit notional;
  slippage worsens price) — adopted identically so FTR numbers are comparable with repo history.
- `db.apply_migrations()` — `0019` rides the existing mechanism.
- Test fixtures conventions (`tests/fixtures/market/*.parquet`), `integration` marker.

**Extended (additive, no behavioral change to existing callers):**
- FastAPI app: new router file + one `include_router` line.
- docker-compose: two new opt-in services under profile `ftr`.
- `.gitignore`, `workers/pyproject.toml` deps.

**New (with justification):**
- FTR backtest engine pair — existing engines are StrategySpec-bound and single-instrument;
  ML-probability replay and multi-asset Decimal portfolio accounting are out of their scope.
  FTR copies the repo's load-bearing conventions (two engines, drift-parity test, next-bar-open
  fill law) instead of modifying engines seven live strategies depend on.
- FTR strategy spec models — `StrategySpec` v2.0 has no vocabulary for ML probability gates,
  portfolio sizing or microstructure inputs. New frozen pydantic models, `model_validate`-only,
  with the UK-retail compliance validator (`spot`-only instruments; perp/future/CFD rejected
  unless `research_simulation_only=True`, and even then refused by the paper trader by type).

## 3. Venue profiles ↔ legacy cost model reconciliation

Legacy (per side, taker): BTC/USDT binance_spot = 10 bps fee + 5 bps "slippage" (the 5 bps bundles
spread + impact). Round-trip 30 bps.

FTR profiles (per side, taker): fee + half_spread + slippage as **separate** terms:

| profile | fee | half-spread (BTC) | slip | round-trip (taker) | uk_execution_feasible |
|---|---|---|---|---|---|
| binance_spot_reference | 10 | 1.0 | 2.0 | 26 bps | false (geo-blocked UK retail) |
| kraken_pro_uk_tier0 | 40 | 2.0 | 3.0 | 90 bps | true |
| coinbase_advanced_uk_tier0 | 60 | 2.0 | 3.0 | 130 bps | true |

No double counting: when an FTR backtest names a profile it uses ONLY that profile's three terms;
it never additionally applies the legacy FeeModel/SlippageModel. The legacy 30 bps figure is
reported alongside `binance_spot_reference` (26 bps) in verdict artifacts for cross-era
comparability; the difference (legacy is 4 bps more pessimistic) is noted in the report.
Cost sensitivity multipliers ×{1.0, 1.5, 2.0} scale the whole per-side sum.

## 4. Postgres migration plan

`0020_ftr_research.sql` creates (all new, no changes to existing tables):
`ftr_decisions` (full DecisionRecord incl. skips; idempotency key (strategy_id, symbol, bar_ts)),
`ftr_orders`, `ftr_fills`, `ftr_positions`, `ftr_equity_snapshots`,
`ftr_model_registry`, `ftr_data_quality`, `ftr_verdicts`, `ftr_killswitch` (single-row flag table).
Applied by the existing `apply_migrations()` on worker/trader boot; rollback = drop ftr_* (no
foreign keys into existing tables; fully detachable).

## 5. How FTR strategies meet the gauntlet

The mandate's G1–G9 gates are the FTR verdict mechanism (Stage 4), computed per
(strategy × venue profile) on stitched OOS results. Where the repo gauntlet has an equivalent,
FTR reuses its convention: DSR via `deflated_sharpe()` (T in years; honest n_trials = total sweep
cells per family); walk-forward fold accounting mirrors `walk_forward.py` shapes; sweep-plateau
G7 generalizes `parameter_sweep.py` peakiness. The repo's "seedable" Sharpe context: G2 uses
net OOS Sharpe ≥ 0.8 (the repo composite has no strict Sharpe floor, so 0.8 stands).
Verdicts: `PASS`, `PASS_LOW_FREQUENCY`, `CONDITIONAL_PASS_INFEASIBLE_VENUE`, `REJECTED`,
`INSUFFICIENT_DATA` — persisted to `ftr_verdicts` with failed-gate lists, served at
`GET /ftr/verdicts`.

## 6. Risks

1. **Compute**: ML walk-forward (≥12 folds × horizons × 2 models) + trend sweeps (≈144 cells × 8
   assets) run locally; mitigated by the vectorized engine and capped XGBoost size. If wall-clock
   explodes, grids stay as specified but folds run sequentially with progress logging.
2. **1m history volume** (~180d × universe ≈ 3.4M rows): parquet-cached, resumable; storage cost
   documented in the data manifest (~10 MB/symbol snappy).
3. **L2 history does not exist publicly for spot** — recorder collects forward only; OFI verdict
   will be `INSUFFICIENT_DATA` until ≥28 recorded days at ≥95% uptime. Expected and specified.
4. **Universe survivorship**: monthly point-in-time re-selection uses only bars existing at
   selection time; assets listed later enter only once they have 540d history. Binance listing
   dates inferred from first available candle — documented limitation (delisted coins absent from
   today's API are invisible; flagged in the final report).
5. **Venue fee schedules drift**: profile values are config, marked "verify at runtime"; the
   validation report names the profile + values used.
6. **Determinism with xgboost `hist`**: enforced `nthread=1`, fixed `random_state`, golden-file
   test; if bit-drift across library versions is detected the golden test pins the version.
