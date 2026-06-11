# Phase B complete — lower timeframes (2026-05-23)

## TL;DR

Phase B — lower timeframes — is **code-complete and live on `main`**.
Ten sub-phases (B.1 → B.10) shipped in a single day on 2026-05-22 →
2026-05-23, all directly to `main` (no v2 branch this time). The
trader now ingests and evaluates strategies at **4H + 1H + 15m** in
parallel; backtest fees + slippage flow through dedicated
`FeeModel` / `SlippageModel` abstractions; the daily drift analyzer
scales its deviation bands per timeframe via a sqrt(N) Brownian
factor; pre-fetched 1H + 15m parquet fixtures keep CI perf-regression
tests reproducible offline.

Phase 1 finding from the design pass held throughout: **the
architecture was already TF-agnostic** (the only `TRADER_TIMEFRAMES`
consumers were trader_worker's ingestion + signal_engine, both
iterating the env-var-driven set). Phase B was therefore about
**cost-model honesty and observability tuning at higher cadence** —
not architectural rework. Two textbook strategies (EMA crossover at
1H in B.7, Bollinger + EMA-200 regime at 15m in B.9) were extracted
and ran the full pipeline; the gauntlet correctly rejected both for
clear statistical reasons. The pipeline IS proven end-to-end at both
new timeframes — the seeded-and-trading leg waits for the first
strategy that actually clears the gauntlet at a lower TF, which is
strategy-hunting work for after Phase B.

The 3 Phase A strategies (BB Breakout, Golden Cross, Modern Turtle —
all 4H) ran throughout Phase B with **bit-identical** behaviour;
Modern Turtle warmup ticked naturally from 214/255 to 218/255 across
the day with no resets.

## Sub-phase summary

| Sub-phase | What shipped | Commits | Key acceptance |
|-----------|--------------|--------:|----------------|
| **B.1** FeeModel | `FeeModel` Protocol + `StaticFeeModel` + tiered table; engine + iterative + benchmark all route through it; spec.costs.commission_pct becomes decorative | 5 | `test_engines_consume_fee_model.py` first-trade comparison |
| **B.2** SlippageModel | Sibling to B.1 — `SlippageModel` Protocol + `StaticSlippageModel`; same three engine sites; 5 bps default (half the FeeModel's 10 bps — intentional asymmetry, explicitly asserted) | 5 | `test_engines_consume_slippage_model.py` |
| **B.3** 1H ingestion | `TRADER_TIMEFRAMES` config-only change `"4h"` → `"4h,1h"`; trader_worker rebuilt (deliberate, first time the brief-anticipated worker rebuild was needed); 1H candles accumulate for BTC/USDT + ETH/USDT | 2 | first-cycle ingest 199 1H bars per symbol, 4H bit-identical |
| **B.4** 1H backtest perf | `tests/fixtures/market/btc_usdt_1h.parquet` (2.6 MB, 55,912 bars); `test_iterative_perf_1h.py` asserts < 5 s; Modern Turtle iterative engine **1.05 s median**, linear 4.08× scaling vs 4H | 1 | perf test green; 4H ~0.256 s baseline preserved |
| **B.5** 1H trader cycle | Live observation via synthetic always-HOLD 15m test version inserted by SQL + watched across 5 cycles + CASCADE-deleted; intersection gate accepts 1H, mixed 4H + 1H coexists, dedup is shared SQL path | 1 | live cycle log evidence; no orphan rows |
| **B.6** drift sqrt(N) scaling | `sqrt_n_scaling_factor(tf)` + `scaled_thresholds(tf)` helpers; per-TF deviation bands (4H identity, 1H 2×, 15m 4×, 1d ≈0.41×); `_classify_health` gained 3 optional kwargs with defaults preserving pre-B.6 behaviour | 1 | 28 existing drift tests pass unmodified; 11 new tests |
| **B.7** 1H seedability | Full pipeline run on EMA(20)/EMA(50) crossover + ATR trailing stop 1H — extraction $0.113, backtest 513 trades in 1.99 s engine-only, gauntlet `mixed_signals` (composite 56/100) → no seed. api container rebuild required en route to pick up v1.1 PSAR fields | 1 | pipeline E2E proven; gauntlet rejection clean |
| **B.8** 15m extension | `TRADER_TIMEFRAMES` → `"4h,1h,15m"`; trader_worker rebuild #2 (picking up B.6 helpers as a bonus); 15m fixture (9.6 MB, 223,527 bars); `test_iterative_perf_15m.py` (8 s threshold, **4.44 s median**); cycle verification via synthetic always-HOLD 15m version | 2 | three-TF coexistence proven; linear 17.34× scaling vs 4H |
| **B.9** 15m seedability | Full pipeline run on Bollinger Band mean reversion + EMA-200 hysteresis regime filter (Tier-2) 15m — extraction $0.123, backtest 2,088 trades, gauntlet `likely_overfit` (composite 61/100 with state-aware A.4 weights WF=0.50 / MC=0.10) → no seed | 1 | pipeline E2E proven at 15m; state-aware weights routed correctly |
| **B.10** sign-off | Worker container hygiene rebuild (pre-B.1 → current main); final 1099-test regression sweep on rebuilt fleet; design-doc reality reconciliation (3 small wording fixes); this document; CLAUDE.md update | 4 | 1099 / 1099 tests; ruff + pyright clean; all four drift parity gates green |

## Final test state (B.10 verification, 2026-05-23)

- **Default suite: 1099 passed**, 132 deselected, ~86 s. `ruff` clean,
  `pyright` 0 errors across `api/src`, `workers/src`, `shared/src`.
- **Drift parity gates — all green (5 of 5):**
  - `test_iterative_live_drift_parity` (Turtle prior_signal, two cases)
  - `test_iterative_live_drift_parity_prior_trade` (prior_trade, two cases)
  - `test_drift_parity_supertrend_live_path_matches_backtest`
    (Tier-2 regime_state, one case)
  - All five exercises pass in **20 s combined**.
- **Perf-regression gates — both green:**
  - `test_iterative_perf_1h` (1H Turtle < 5 s)
  - `test_iterative_perf_15m` (15m Turtle < 8 s)
- **v1 regression — bit-identical** across the entire Phase B.
  The 3 Phase A strategies' (BB Breakout, Golden Cross, Modern Turtle)
  trader_strategy_versions rows are untouched; live cycle pattern
  `versions_loaded=3, evaluations=2, holds=2, pair_attempts=3,
  pair_insufficient_history=1` (Modern Turtle warmup) was preserved
  across every B.3 / B.8 trader_worker rebuild and every B.5 / B.8
  synthetic-version insert+delete.

## Architecture finding — Phase B was tuning, not rework

The Phase B design's Phase 1 finding ("the architecture is already
TF-agnostic") held literally true:

- `ingestion.py:404-454` already iterates the Cartesian product
  `TRADER_SYMBOLS × TRADER_TIMEFRAMES`.
- `signal_engine.py:632-634` already gates each version by
  `version.timeframes ∩ config_timeframes`.
- `trader_candles.timeframe TEXT NOT NULL` accepts any TF string
  cleanly — no schema migration was needed.
- The backtest engines (`engine.py` vbt path, `iterative.py` T3 path)
  operate on pandas DataFrames indexed by tz-aware DatetimeIndex with
  arbitrary spacing; the only TF-aware code is `_VBT_FREQ` (a
  one-line dict) and `_BARS_PER_YEAR` (another one-line dict).

Phase B's nine non-trivial sub-phases (B.1-B.9) therefore broke down
into:

- **Three "cost-model honesty" sub-phases (B.1, B.2, B.6)** —
  ridding the codebase of TF-implicit hardcoded numbers (fees,
  slippage, drift thresholds) in favour of small Protocol-backed
  abstractions with explicit per-TF inputs. The B.4-era state-aware
  weighting was a precursor of the same pattern.
- **Three "extend the env" sub-phases (B.3, B.8, and B.10's worker
  rebuild)** — purely operational: change one env var, deliberately
  rebuild one container, observe one cycle, document.
- **Two "verify the engine" sub-phases (B.4, B.8 perf component)** —
  fetch a deeper fixture, write a one-test wall-clock budget assertion,
  document the empirical scaling.
- **Two "synthetic cycle observation" sub-phases (B.5, B.8 cycle
  component)** — insert always-HOLD test version, observe N cycles,
  CASCADE-delete, document.
- **Two "real strategy through gauntlet" sub-phases (B.7, B.9)** —
  extract, backtest, run overfitting analysis, observe the gauntlet
  refuse two textbook strategies.

The whole phase fit in a single day because the architecture was
right.

## Hard-won knowledge

Gotchas, lessons, and meta-patterns from Phase B.

- **Container deployment debt is wider than the Phase A pattern
  suggested.** Phase A's discipline was "rebuild `trader_worker`
  deliberately for in-process state work" — correct for trader-specific
  changes (env vars, scheduled-job lifecycle) but does NOT extend to
  shared-package additions. Phase B surfaced this three times: B.7
  needed an api rebuild for v1.1 PSAR fields; B.8 discovered the worker
  container was pre-B.1 (just produced correct numbers by coincidence);
  B.10 closed the gap with a worker hygiene rebuild. **Operator
  default going forward:** after any indicator-whitelist / schema /
  v2-primitive addition, rebuild ALL THREE Python containers (`api`,
  `worker`, `trader_worker`). Total ~6-8 min back-to-back with
  `--no-deps` on each. Logged in `docs/operations/v1.1-todos.md`.

- **Two textbook strategies, two gauntlet rejections — same pattern
  as Phase A's Supertrend and RSI-pullback rejections.** EMA crossover
  at 1H (B.7) failed for walk-forward degradation 0.0 + no Monte-Carlo
  edge. BB+EMA200 at 15m (B.9) failed for OOS positive_rate 0.0 +
  catastrophic 99.6% drawdown. **The gauntlet doing its job is the
  system working.** The seed-and-trade leg at a lower TF waits for a
  strategy with a real edge — strategy-hunting work for after Phase B.

- **Tier-2 routing first exercised in production via B.9.** The A.4
  state-aware weights (WF=0.50, MC=0.10 vs Tier-1 base 0.35 / 0.25)
  routed correctly via `spec_uses_stateful_v2(spec) == True`. The
  composite contributions in B.9's overfitting result confirm the
  weights are applied as designed. The actual-use check (not
  `schema_version == "2.0"`) is the right gate — re-confirmed.

- **Linear scaling held at 17× density.** Iterative engine perf
  measured across three timeframes on the same Modern Turtle spec:
  4H = 0.256 s (13,985 bars), 1H = 1.090 s (4.26×), 15m = 4.440 s
  (17.34× — within 8% of the linear-pred 16×). Validates the engine's
  per-bar O(indicators × bar) bound. Extrapolating: 5m → ~13 s
  (plausibly within budget); 1m → ~65 s (would warrant attention).
  The deferred "vectorised T3 engine" optimisation listed in B.4 is
  NOT justified.

- **`max_drawdown > 80%` is qualitatively different from
  sharpe-of-noise overfitting.** B.9's strategy lost 99.6% of capital
  but landed at composite 61/100 (`likely_overfit`) — same band as
  B.7's "sharpe-of-noise" 56/100. The composite doesn't distinguish
  "actively destroys capital" from "no real edge." Suggested
  enhancement: soft two-tier flag — 80-95% adds a "capital
  destruction" warning to the explanation, >95% triggers hard
  refusal. Logged as a v1.1 follow-up.

- **`spot` exchange-string quirk** — both B.7 and B.9 extractions
  produced `instrument.exchange = "spot"` instead of `"binance"`.
  Falls through to FeeModel / SlippageModel fallback path, which
  happens to match `binance_spot` defaults exactly for BTC/USDT (10 / 5
  bps), so backtest math was unaffected. **Risk:** any future table
  with non-default fees for a different venue would silently use
  wrong (fallback) numbers. Fix: extraction-prompt teaching audit +
  defensive `_exchange_key()` mapping. Logged in v1.1-todos.

- **`_MIN_WINDOW_BARS = 200` floor on SpecTemplate.** Even a
  no-indicator spec needs 200 closed candles before first evaluation.
  At 4H that's ~33 days; at 1H ~8 days; at 15m ~2 days. The synthetic
  cycle tests in B.5 + B.8 demonstrated this concretely (199 → 200
  bars exactly hit the floor on the next bar close). **Any future
  strategy seeded at a lower TF needs to wait this many bars before
  first live evaluation** — Modern-Turtle-like behaviour but
  applicable to every SpecTemplate spec, not just stateful ones.

- **Fee / slippage default asymmetry (10 / 5 bps).** B.1's FeeModel
  defaults to 10 bps; B.2's SlippageModel defaults to 5 bps. Spreads
  on BTC/USDT majors are tighter than round-trip commission — the
  asymmetry is intentional and explicitly asserted in
  `test_default_model_returns_5_bps_for_btc_usdt_taker`. Easy to typo
  ("they're both costs, they should be the same") — the explicit test
  exists to prevent that.

## Operational state at sign-off

- **3 strategies actively running** in paper-trading: Bollinger Band
  Breakout EMA200 4H BTC, Golden Cross 50/200 SMA 4H BTC, Modern
  Turtle Donchian Breakout 4H BTC (the v2-native `template='spec'`).
- **Modern Turtle warmup: 218/255 bars** at end of B.10. First live
  evaluation still around 2026-05-30 (the v2 state-persistence first
  real exercise).
- **Three-TF ingestion live:** `trader_candles` has rows for BTC/USDT
  and ETH/USDT at all three timeframes (4H, 1H, 15m), accumulating
  forward.
- **Daily summary fires at 00:05 UTC**, writes to
  `data/daily-summaries/daily-summary-YYYY-MM-DD.{json,txt}` — Phase
  A observability still working unchanged.
- **No alerts in last 60 min.** Heartbeat fresh.
- **trader_worker** rebuilt twice during Phase B (B.3 1H, B.8 15m);
  **api** rebuilt once (B.7 for PSAR fields); **worker** rebuilt once
  (B.10 hygiene). All three containers now at current main.

## What Phase B did NOT decide (deferred)

- **Exact fee tier values for non-default venues.** B.1's
  `_DEFAULT_FEE_TABLE` covers Binance Spot BTC/USDT at 10 bps and
  uses a conservative-pessimist fallback for everything else. Real
  per-exchange / per-symbol tiers (Binance VIP volume tiers, Coinbase
  Prime, etc.) are out of scope until either a non-Binance strategy
  ships or the operator's 30-day notional exceeds VIP 1 (~$1M
  monthly). Refresh procedure in `docs/operations/fees.md`.
- **Slippage parameter calibration from live data.** B.2's
  `_DEFAULT_SLIPPAGE_TABLE` uses v1's 5 bps default. The honest source
  for slippage values is the trader's own `trader_fills
  .slippage_bps_applied` column (migration 0008) — once enough fills
  accumulate, compare realised vs the static table and refresh.
  Procedure in `docs/operations/slippage.md`.
- **Drift-threshold empirical tuning at 1H / 15m.** B.6's MULTIPLY-by-
  sqrt(N) is conservative for per-trade-average metrics (the noise
  actually shrinks 1/sqrt(N) at higher cadence, so wider bands
  under-flag). Empirical tuning waits for the first 1H or 15m
  strategy to actually clear the gauntlet and accumulate ≥5 paper
  trades — then a month of `trader_drift_metrics` health rows tells
  us whether the bias is acceptable. If under-flagging dominates,
  switch to DIVIDE-by-sqrt(N) for per-mean metrics. Plan documented
  in design doc §3 B.6.
- **Per-strategy drift drawdown.** Currently uses portfolio-wide
  drawdown as a proxy (drift.py docstring + design doc). Per-strategy
  drawdown trajectory would require walking
  `trader_portfolio_snapshots.per_strategy_breakdown` history — v2
  follow-up.
- **Multi-asset (Phase C).** B.3+B.8 add 1H and 15m for BTC/USDT and
  ETH/USDT; no other symbols. Multi-asset portfolio composition,
  position-sizing across symbols, and per-symbol risk caps are Phase C
  scope (deferred).
- **Live execution (Phase D).** All Phase B work remains paper-only
  per the `assert_paper_only()` hard guard. Live execution is gated on
  far more than a date.

---

## Phase B done state

The trader can now ingest, backtest, and evaluate strategies at **4H,
1H, and 15m** in parallel. Fees and slippage flow through small,
testable abstractions (`FeeModel`, `SlippageModel`) — adding a new
exchange or refreshing a tier is a one-file change with a documented
procedure. The drift analyzer scales its deviation bands per timeframe
without breaking the 4H bit-identity gate. Two real-strategy
pipeline tests at lower TFs (B.7 + B.9) demonstrated the full
extract → backtest → gauntlet flow; the gauntlet correctly refused
both because they're not paper-worthy.

Phase B is verification-complete and signed off. The first 1H or 15m
strategy that clears the gauntlet would slot into the trader exactly
the same way the existing 4H strategies did — `version.timeframes[0]`
just changes the cadence. That's a strategy-hunting exercise for
whoever picks it up next, not a Phase B blocker.
