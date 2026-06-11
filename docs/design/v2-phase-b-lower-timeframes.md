# v2 Phase B — lower timeframes (1H, 15m)

Design doc; no code. Implementation follows in sub-phase commits across
subsequent sessions after this design is reviewed and signed off.

## §1 Scope and motivation

Phase B extends MarketMind to two new timeframes on BTC/USDT: **1H** and
**15m**. The frequency multiplier (vs the 4H baseline that Phase A
shipped) is 4× and 16×. The thesis is simple: **lower timeframes
collapse the fee/slippage error bars that 4H lets us hide**. At 4H,
~6 trades/day × ~10 bps round-trip ≈ ~12 bps daily drag — a rounding
error against the strategy's directional edge. At 15m, the same flat-bps
model implies ~96 trades/day × ~10 bps ≈ ~200 bps daily drag, which
swamps every realistic edge unless the cost model is honest. **Phase B
is therefore as much about refitting the cost model as about ingesting
faster candles.**

Hard constraints carry over from Phase A unchanged:

- Paper-only. `assert_paper_only()` is the runtime guard; `TRADER_ALLOW_LIVE`
  must be `false` at trader boot.
- Long-only spot, BTC/USDT for the v1.x line.
- The "experimenting not researching" discipline: the user has dropped
  the originally-proposed 30-day paper observation gate. Reasoning:
  4H itself was built without prior live data, the paper bot means no
  real risk, and shipping the lower-TF path in parallel with the
  ongoing 4H observation is sound. Phase A observation continues; it
  does not block Phase B.

## §2 Phase 1 findings — how deeply is 4H baked in?

Discovery on the post-Phase-A `main` (HEAD `fa06de8`). **The
architecture is parameterized; 4H is the DEFAULT, not the constraint.**
Headlines:

| Surface | State | Notes |
|---|---|---|
| `Timeframe` enum (`shared/.../strategy_spec/common.py`) | **All 7 timeframes already enum members**: M1, M5, M15, M30, H1, H4, D1 | No whitelist gating |
| `trader_candles` schema (`infra/db/migrations/0007`) | `(symbol, timeframe, open_ts, close_ts, ohlcv)`; index on `(symbol, timeframe, close_ts DESC)` | **Multi-timeframe-capable already**; no schema change needed |
| Trader ingestion (`trader/ingestion.py`) | Iterates `TRADER_SYMBOLS × TRADER_TIMEFRAMES`; calls `fetch_ohlcv` per pair | **Already multi-TF**; just env-var-driven |
| `TRADER_TIMEFRAMES` default | `"4h"` (compose env `${TRADER_TIMEFRAMES:-4h}`) | Easy override |
| Trader main-cycle cadence (`trader/runner.py`) | `next_minute_boundary` → cycle every 60 s, independent of strategy TF | Fine for every TF down to 1m (no rescheduling needed) |
| `services/market_data.py` whitelist | `{"1m","5m","15m","30m","1h","4h","1d"}` accepted | No 4H bias |
| Bar-duration lookups (`shared/trader/time.py`, `services/market_data.py`) | All 7 timeframes listed | Complete |
| Backtest engine (`backtest/engine.py`) | `Timeframe → pandas freq` mapping has all 7 | TF-agnostic iterator |
| Walk-forward (`overfitting/walk_forward.py`) | All 7 timeframes seconds-mapped | TF-agnostic |
| Observability warmup-ETA lookup (`observability/queries.py`) | `{"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}` | **Missing 1m/5m/30m** — minor, only matters if those TFs are ever used (Phase B keeps them out of scope) |

**Genuinely 4H-specific code paths** (i.e. would need changes for 1H/15m
to work *correctly*, not just *run*): none found. Every 4H reference in
production code is either (a) a default in an env var or seed script, or
(b) a lookup table that already includes 1H and 15m.

The substantive Phase B work is therefore **not "un-bake 4H"** but:

1. **Cost-model honesty.** The current flat `10 bps fee + 10 bps
   slippage` is a v1 approximation. It is fine for 4H (the cost is
   small relative to the per-trade edge); it materially distorts 15m
   results. The model needs to express realistic per-exchange / per-
   timeframe / per-volume costs.
2. **Higher-frequency observability.** Drift thresholds tuned for ~6
   trades/day will over-trigger at ~24 trades/day (1H) or ~96/day
   (15m). Threshold scaling needs a thoughtful answer.
3. **Performance verification.** A 6-year backtest at 15m is ~210 k
   bars vs the current ~13 k at 4H. The iterative engine is loop-bound
   in Python; needs measurement.
4. **A first-1H strategy seed proof.** End-to-end validation of the
   parameterised path on a real strategy.

That reframes the sub-phase ordering — the user's original layout is
broadly correct; the verification sub-phases (B.3–B.5) are smaller than
they looked because the architecture already handles them.

## §3 Sub-phase proposal

10 sub-phases, ordered. Session estimates are rough (1 session = a
focused multi-commit batch like Phase A's A.5a or the post-Supertrend
batch); calendar will depend on how often sessions happen.

### B.1 — Fee model refinement (foundation)
- **Ships:** a `FeeModel` abstraction in `trader/execution.py` (and
  mirror in `backtest/`) that can express more than a flat bps. Initial
  shape: a per-exchange tier table (`maker_bps`, `taker_bps`, with
  volume-tier breakpoints). Default exchange = Binance, default tier =
  "VIP 0" with current 10 bps to preserve v1 behaviour exactly. Per-
  version override unchanged (existing `trader_strategy_versions.fee_bps`
  becomes a *floor / override* on the model output).
- **Deps:** none.
- **Sessions:** 1–2.
- **Acceptance:** existing 4H strategies' fee numbers identical (10 bps
  in, 10 bps out — bit-identical fill ledger); a unit test for a
  hypothetical "VIP 3" tier returning different numbers; ruff/pyright
  green; full suite green.
- **Deferred:** live fee scraping from an exchange API; per-pair fee
  variation (BTC/USDT has the same fees as ETH/USDT on Binance); fee
  rebates from market-making.
- **Shipped (2026-05-22, commits `05e382a` → `0910c2d`):**
  - `workers/src/marketmind_workers/backtest/fee_model.py` — `FeeModel`
    Protocol, `StaticFeeModel` backend, `FeeTier` tier row, `FeeTable`
    nested-dict (`exchange → symbol → side → tiered list`),
    `default_fee_model()` (Binance Spot BTC/USDT 10 bps both sides),
    `commission_for_spec()` convenience that resolves
    `instrument.exchange` (e.g. `"binance"` → `"binance_spot"`).
  - `workers/src/marketmind_workers/backtest/engine.py` +
    `iterative.py` + `jobs/backtest.py` — all three sites that used to
    read `spec.costs.commission_pct` now go through `commission_for_spec`.
    The benchmark buy-and-hold uses the same fee path.
  - `spec.costs.slippage_pct` still flows through unchanged (B.2 ships
    the `SlippageModel` sibling).
  - **Bit-identity proof:** the existing 1070-test suite runs green
    with no expected-value tweaks. The three seeded strategies
    (BB Breakout, Golden Cross, Modern Turtle) all carry
    `commission_pct == 0.001` in their CostModel, and the default
    `FeeModel` returns exactly that for Binance BTC/USDT — so every
    backtest, every dashboard number, every drift parity gate is
    byte-stable across the swap.
  - **Non-default-fee proof:** new test
    `workers/tests/test_engines_consume_fee_model.py` swaps the
    iterative engine's `default_fee_model` for a 50 bps StaticFeeModel
    and asserts the first trade's `return_pct` strictly drops — the
    first trade is gate-independent, so it isolates the fee delta from
    any prior_signal / prior_trade gating side-effects. (For Turtle
    System 1 the higher fee also shifts phantom-trade outcomes, which
    propagates into the skip-after-winner gate and changes the
    downstream ledger — that's the gate working correctly, not a test
    artefact.)
  - **Operator-facing docs:** `docs/operations/fees.md` documents the
    quarterly manual refresh procedure (per Q1 resolution: static
    table, not live API; revisit if/when Phase D).
  - **Trader path untouched.** `trader_strategy_versions.fee_bps`
    remains the trader's authoritative per-version override; the
    floor/override-on-FeeModel-output unification is deferred to a
    later phase (out of B.1 scope per the brief).
  - **Test count:** 1070 → 1078 (7 fee_model unit tests + 1
    non-default-fee integration test).
  - **Suite status:** ruff clean, pyright clean, full suite green,
    all four drift parity gates (Supertrend, Turtle, prior_signal,
    prior_trade) green.

### B.2 — Slippage model refinement
- **Ships:** a `SlippageModel` abstraction parallel to `FeeModel`.
  Default: a spread-based model assuming `BTC/USDT` major-pair spread of
  ~1 bps + a market-impact term scaled by trade-size-fraction-of-bar-
  volume. Defaults preserve the current 10 bps behaviour for the
  existing strategies; the abstraction enables per-TF / per-trade-size
  scaling.
- **Deps:** B.1 (the two interfaces are sibling).
- **Sessions:** 1.
- **Acceptance:** existing 4H strategies' slippage numbers identical
  under the default; unit test for a "small trade vs large trade" pair
  producing different slippage; ruff/pyright green; full suite green.
- **Deferred:** live order-book L2 data; venue-specific slippage
  curves; stop-fill slippage variability under volatility regimes.
- **Shipped (2026-05-23, commits `9979971` → `4945fd5`):**
  - `workers/src/marketmind_workers/backtest/slippage_model.py` —
    `SlippageModel` Protocol, `StaticSlippageModel` backend,
    `SlippageTier` tier row, `SlippageTable` nested-dict
    (`exchange → symbol → side → tiered list`),
    `default_slippage_model()` (Binance Spot BTC/USDT 5 bps both
    sides), `slippage_for_spec()` convenience that resolves
    `instrument.exchange` (e.g. `"binance"` → `"binance_spot"`).
    Sibling to `fee_model.py` shape-for-shape — same Protocol /
    Backend / Tier / loader / mapping pattern.
  - **Asymmetric default vs FeeModel.** SlippageModel default is
    **5 bps** (half the FeeModel's 10 bps). Spreads on BTC/USDT
    majors are tighter than round-trip commission — the asymmetry is
    intentional and explicitly asserted in the unit test
    `test_default_model_returns_5_bps_for_btc_usdt_taker`. Easy to
    typo (assumed symmetric with fees) hence the load-bearing test.
  - `workers/src/marketmind_workers/backtest/engine.py` +
    `iterative.py` + `jobs/backtest.py` — same three sites B.1
    touched. The vbt path's `Portfolio.from_signals(slippage=…)`,
    the iterative path's `_resolve_costs` (which is also reused by
    `iterative_live.py` via import), and the benchmark
    buy-and-hold's `slippage_pct=…` all now derive from the
    SlippageModel.
  - **`spec.costs` is now fully decorative for the engine** (B.1
    moved commission, B.2 moves slippage; the spec field stays for
    UI display and serialisation, but neither field is read by the
    engine). The schema docstring in
    `shared/.../strategy_spec/costs.py` records this explicitly.
  - **Bit-identity proof:** the existing 1078-test suite (after
    adapting two tests that intentionally probed the spec.costs
    path) plus the 7 new SlippageModel unit tests + 1 new
    non-default-slippage integration test = **1086 tests green**.
    The three seeded strategies carry the v1 default
    `slippage_pct = 0.0005`, and the SlippageModel default returns
    exactly that for Binance BTC/USDT — bit-identical backtests.
  - **Non-default-slippage proof:** new test
    `workers/tests/test_engines_consume_slippage_model.py` swaps the
    iterative engine's `default_slippage_model` for a 20 bps
    StaticSlippageModel and asserts the first trade's `return_pct`
    strictly drops — same gate-independent first-trade comparison
    pattern as B.1's commit `0910c2d`. Gate-induced ledger
    divergence on cost-model perturbation is now an established
    finding; B.3+ work will see the same pattern.
  - **Two pre-existing tests adapted** as a direct downstream
    consequence of the spec.costs path becoming decorative:
    `test_engine.py::test_costs_reduce_final_equity` migrated to
    monkeypatched FeeModel + SlippageModel injection (same contract,
    new abstraction surface). `test_iterative.py
    ::test_prior_signal_phantom_outcome_matches_the_real_trade` got
    its hardcoded `slippage=0.0` bumped to `slippage=0.0005` to
    match the new engine default. Both adaptations are recorded in
    commit `9932b40`'s message.
  - **Operator-facing docs:** `docs/operations/slippage.md`
    documents the quarterly manual refresh procedure (sibling to
    `fees.md`, same shape, surfaces the fee/slippage asymmetry
    explicitly).
  - **Trader path untouched.** `trader_strategy_versions.slippage_bps`
    remains the trader's authoritative per-version override — same
    finding as B.1's `fee_bps`. Trader and backtest paths are
    independent; unification (model output as floor / per-version
    as override) is deferred to a future phase, out of B.2 scope.
  - **Test count:** 1078 → 1086 (+8 total: 7 SlippageModel unit
    tests in commit 1 + 1 non-default-slippage integration test in
    commit 4).
  - **Suite status:** ruff clean, pyright clean, full suite green,
    all four drift parity gates (Supertrend, Turtle, prior_signal,
    prior_trade) green.

### B.3 — 1H candle ingestion verification
- **Ships:** add `"1h"` to the `TRADER_TIMEFRAMES` env var; run trader
  for ~24 h observation; verify candles ingest cleanly + backfill works
  + no schema changes needed.
- **Deps:** none (the architecture already supports it).
- **Sessions:** 0.5.
- **Acceptance:** `trader_candles` rows for `(BTC/USDT, 1h)` accumulate
  at the right rate; no `ccxt` errors; no schema migrations needed.
- **Deferred:** other symbols; sub-1H ingestion.
- **Shipped (2026-05-23, commits `b5e4d8e` → this commit):**
  - **Config-only change.** `trader_timeframes` default flipped from
    `"4h"` → `"4h,1h"` in all three sources: Python default
    (`workers/.../trader/config.py`), compose default
    (`docker-compose.yml`), example env (`infra/.env.example`).
  - **No code change in ingestion or signal_engine** — both already
    iterate over `settings.timeframes_list()` (the Phase 1
    architecture finding from the design doc §1 table held up
    exactly: "Already multi-TF; just env-var-driven").
  - **No schema migration.** `trader_candles.timeframe TEXT NOT NULL`
    accepts `"1h"` cleanly (migration `0007`).
  - **Trader_worker rebuild.** Brief assumed the env var change would
    only require `worker` restart, but Phase 1 surfaced that
    `TRADER_TIMEFRAMES` is consumed inside `trader_worker` (both
    ingestion + signal_engine). User-approved deliberate rebuild via
    `docker compose up -d --build --no-deps trader_worker`. Image
    build ~3 min, container recreate <10 s; clean boot, scheduler
    re-acquired, idempotent bootstrap (`scheduled={}` — all 5 ticks
    were already in the registry).
  - **First post-rebuild cycle:**
    `ingest_cycle_complete candles_inserted=398, pairs_succeeded=4,
    backfill_attempts=0, gaps_detected=0` — 4 pairs ingested
    cleanly; 398 = (199 BTC/USDT 1h + 199 ETH/USDT 1h) + 0 BTC 4h
    (already present) + 0 ETH 4h (already present). The 1H rows
    span 2026-05-15 10:00 → 2026-05-23 16:00 (~8.3 days),
    consistent with `fetch_recent_ohlcv(limit=200)` minus the
    currently-unclosed bar.
  - **Backfill depth observation.** First ingest pulls 200 most
    recent 1H bars (≈ 8 days). Wider backfill (limit=1000, ~41 days)
    only fires if `gaps_detected > 0` — none triggered (clean data
    feed). For the 6-years-of-1H-bars perf-test target in B.4, the
    `/data` Parquet cache (Phase 3 market_data service) is the
    source, **not** `trader_candles` — that cache already has 1H
    coverage from the Phase 3 ingestion path. `trader_candles` is
    the trader's runtime view; it doesn't need 6-year depth.
  - **Second cycle clean:** `candles_inserted=0` (no new bar
    closes), `signal_cycle evaluations=2 holds=2
    pair_insufficient_history=1` — IDENTICAL to pre-rebuild pattern.
    Modern Turtle warmup stable at 218/255 bars.
  - **v1 regression: bit-identical.** 4H row counts unchanged (218
    BTC + 218 ETH = 436 total, matching pre-rebuild). Modern Turtle
    warmup unchanged at 218 bars. All 3 versions still active,
    enabled, approved-for-paper. No alerts. Heartbeat fresh.
  - **Test impact.** `test_trader_settings_defaults_are_loaded`
    updated to assert `["4h", "1h"]`. The test was also made truly
    hermetic with `TraderSettings(_env_file=None)` — pre-existing
    fragility: `monkeypatch.delenv` only clears shell env, leaving
    pydantic-settings to load the dev `.env` file silently. Surfaced
    by this default change; pre-existing fragility closed in the
    same commit.
  - **Test count:** 1086 → 1086 (no new tests; one assertion update).
    Suite green, ruff clean, pyright clean.
  - **Trader-side parallel concern.** The signal_engine
    intersection gate (`version.timeframes ∩ config_timeframes`)
    means the 3 currently-seeded 4H strategies keep evaluating
    only on 4H. 1H rows accumulate as foundation for B.7+ (the
    first 1H strategy seedability proof); no live evaluation
    against them happens yet.

### B.4 — 1H backtest performance verification
- **Ships:** a perf-regression test that runs a representative 1H
  strategy backtest on 6 years of 1H bars (~52 k bars) and asserts
  runtime under a documented budget. If the iterative engine is too
  slow, a vectorized optimisation pass.
- **Deps:** B.3 (need 1H data).
- **Sessions:** 0.5–1.
- **Acceptance:** 1H Turtle backtest runs in <2 s (the current 4H is
  ~0.355 s; 4× bars at the same per-bar cost suggests ~1.5 s); same
  test runs in CI; ruff/pyright green; full suite green.
- **Deferred:** further perf optimisation; vectorised T3 engine
  (only worth it if the perf-regression test fails the budget).
- **Shipped (2026-05-23, single commit):**
  - **1H fixture committed** at
    `tests/fixtures/market/btc_usdt_1h.parquet` — 55,912 bars
    spanning 2020-01-01 00:00 → 2026-05-19 23:00 UTC, ~2.6 MB
    compressed. Fetched once via `get_market_data()` (Binance ccxt
    path, 56 pages, ~25 s of real network traffic, one-time cost).
    Mirror of the existing 4H fixture pattern — same data shape
    (open/high/low/close/volume), same DatetimeIndex tz, same
    symbol coverage. Phase 1 finding: production `/data/cache` and
    test fixtures both had only the 4H parquet; 1H side had never
    been triggered. Brief's stop-trigger fired on this — user
    approved the fetch-and-commit-as-fixture path.
  - **Perf-regression test** at
    `workers/tests/test_iterative_perf_1h.py`. Runs Modern Turtle
    System 1 (same `prior_signal`-gated spec used by every other
    drift-parity / engine test) with `primary_timeframe`
    overridden to `"1h"`. Single timed run, asserts wall-clock
    under **5 s**. Sanity-asserts non-empty trade ledger so a
    future bug that nukes trades can't masquerade as good perf.
    Runs in the default suite (no marker — total wall-clock ~2 s
    including parquet load; comparable to existing
    drift-parity tests).
  - **Measurement (local, three warm runs each):**
    - **4H Turtle:** min 0.225 s, median 0.256 s, max 0.328 s on
      13,985 bars → 160 trades. **Faster than the 0.355 s
      baseline cited in design Q4** (likely a measurement-context
      difference: warmer cache, faster CPU; not a regression
      either way).
    - **1H Turtle:** min 0.985 s, median 1.046 s, max 1.047 s on
      55,912 bars → 665 trades. **~4.1× the 4H runtime** for **4×
      the bars** — essentially linear scaling. No algorithmic
      regression at 1H density.
  - **Performance verdict:** **meets the 2 s design target** with
    ~2× headroom on median runtime. The deferred "vectorised T3
    engine" pass is not justified — linear scaling at 1H means the
    iterative engine will tolerate 15 m (~210 k bars,
    extrapolating linearly → ~4 s) without architectural change.
    Re-evaluate at 15 m if the actual median climbs above ~3 s.
  - **Threshold reasoning:** 5 s = ~5× median headroom — absorbs CI
    runner variance (shared-core hosts, cold caches, GIL
    contention) without being so loose that a real regression
    slips through. Tightening to ~3 s is plausible after a few
    green CI runs; deliberately starting permissive. **The
    early-warning signal is the median creeping above ~2 s** (2×
    today's), not the threshold tripping — that's the cue to
    investigate before it starts flaking.
  - **v1 4H regression: clean.** The four drift-parity gates +
    both engine-consumes-{fee,slippage} tests + Turtle integration
    all pass in 20 s combined. 4H runtime matches the linear
    baseline (4H median 0.256 s, 1H median 1.046 s, ratio 4.08
    against bar-count ratio 4.00 — within noise). FeeModel +
    SlippageModel abstractions add no observable overhead.
  - **Test count:** 1086 → 1087 (+1 perf test). Suite green
    (1087 in 82 s), ruff clean, pyright clean.
  - **`get_market_data()` host-vs-container permission gotcha.**
    The production `/data/cache/market/BTC_USDT/` dir is owned by
    UID 999 (docker user); host `uv run` invocations as user
    the host user can't write there. The fixture-fetch script worked
    around it via `tempfile.TemporaryDirectory()` as the
    `data_dir` arg. Future host-side fetches that want to
    populate the production cache will need either a chown or
    `docker exec` into the worker container. Logged as a
    documentation follow-up for `v1.1-todos.md` if it recurs.

### B.5 — 1H trader cycle verification
- **Ships:** verification that the signal engine evaluates 1H strategies
  at 1H candle-close boundaries (not mid-bar). Likely a small check in
  `signal_engine` that the candle's `close_ts` is at-or-before the
  current cycle's tick. If the existing dedup logic already handles
  this (likely — `signal_pair_duplicate_signal` exists), the sub-phase
  reduces to adding a 1H-specific test.
- **Deps:** B.3.
- **Sessions:** 0.5.
- **Acceptance:** a 1H spec with a deterministic signal fires exactly
  once per 1H candle close (not 60×); no `pair_duplicate_signal`
  warnings beyond expected dedup; ruff/pyright green; full suite green.
- **Deferred:** sub-1H cycle scheduling (15m specific verification in
  B.8).
- **Shipped (2026-05-23, verification-only — no code change, no commit
  to runtime):**
  - **Approach:** insert a synthetic minimal 1H spec directly into
    `trader_strategy_versions` (template=`spec`, always-HOLD entry,
    BTC/USDT @ 1h, `approved_for_paper=true, enabled=true`), watch
    the trader cycle for several minutes, then `DELETE` the parent
    `trader_strategies` row (CASCADE handles versions / state /
    signals / orders / positions / drift_metrics). The Phase 1
    discovery showed that B.5's design-doc hypothesis held exactly:
    cycle scheduling is timestamp-based (not wall-clock cron) and
    the dedup mechanism is fully timeframe-agnostic, keyed on the
    `(version, symbol, timeframe, candle_close_ts)` tuple. The
    existing `test_cycle_dedupes_repeated_runs_on_same_candle` test
    in `test_trader_signal_engine.py:431` already covers dedup
    transitively — no new dedup-at-1H test needed.
  - **Pre-existing test coverage:** `_signal_exists()` uses the same
    SQL key for every timeframe; the live behaviour at 4H has been
    correct for 24+ hours of paper-bot uptime, so dedup-at-1H is
    proven by code-path identity, not by re-running a 1H-specific
    test.
  - **Synthetic spec.** A 1H `compare(close, >, 999_999_999)` →
    always-false entry. Parses cleanly through `StrategySpec`,
    builds cleanly through `SpecTemplate` (min_bars_needed = 200,
    the `_MIN_WINDOW_BARS` floor). Always returns HOLD, so no
    `trader_signals` rows are ever written — no downstream
    orders/positions/state to clean up afterwards.
  - **Live cycle observations (5 cycles, 2026-05-23 17:39–17:44
    UTC):**
    - At 17:39 the new version was loaded into the cycle for the
      first time. The 17:00 UTC 1H bar closed during the preceding
      ingest, bringing `trader_candles WHERE timeframe='1h'` from
      199 → 200 rows, exactly hitting `min_bars_needed`. The first
      eligible cycle therefore evaluated the test version cleanly;
      no `pair_insufficient_history` for the 1H pair (only Modern
      Turtle's 4H 218/255 entry remained).
    - 17:39, 17:40, 17:41, 17:42, 17:43 all showed identical
      `versions=4, versions_loaded=4, evaluations=3, holds=3,
      pair_attempts=4, versions_misconfigured=0, pair_no_data=0,
      pair_state_disabled=0, signals_persisted=0`. The test
      version's `(BTC/USDT, 1h)` pair was attempted and evaluated
      each cycle, returning HOLD as designed.
    - 17:44 (first cycle post-DELETE) reverted to `versions=3,
      evaluations=2, holds=2, pair_attempts=3,
      pair_insufficient_history=1` — bit-identical to the
      pre-insert pattern from earlier in the day. Modern Turtle
      warmup stable at 218/255 throughout (no synthetic state
      created, no warmup counter perturbed).
  - **What this verifies:**
    - The signal engine's intersection gate (`version.timeframes ∩
      config_timeframes` at `signal_engine.py:632-634`) correctly
      includes a 1H version when `TRADER_TIMEFRAMES = "4h,1h"`.
    - Mixed-TF version loading is correct (3×4H + 1×1H versions
      coexist; no misconfig).
    - `_latest_closed_candle()` for a 1H pair returns the
      most-recent closed 1H bar without conflict from the
      simultaneously-running 4H ingest.
    - `_load_candles_df(symbol, '1h', fetch_bars)` returns the
      right shape; the SpecTemplate evaluator runs cleanly on 1H
      bars.
    - The 4H code path is bit-identical to before B.5
      (versions_misconfigured, pair_attempts, evaluations, holds,
      pair_insufficient_history, Modern Turtle warmup all
      unchanged on the 4H side throughout the test).
  - **What this does NOT verify** (documented; covered elsewhere):
    - **Dedup at 1H cadence.** The always-HOLD spec never persists
      a signal, so `_signal_exists` never returns True for it. The
      brief explicitly forbade firing real signals from a
      synthetic version (avoids orphan
      orders/positions/portfolio-snapshots). Dedup is the same SQL
      query for all timeframes — covered transitively by
      `test_cycle_dedupes_repeated_runs_on_same_candle`.
    - **"Only 1H evaluates at non-4H 1H boundary"** semantic. With
      always-HOLD specs, every version evaluates every cycle
      regardless of boundary (no signal to dedup). This semantic
      only becomes observable when specs fire signals; B.7's first
      seeded 1H strategy will demonstrate it organically once
      enabled.
  - **`_MIN_WINDOW_BARS = 200` floor finding** (worth recording for
    B.7+): even a no-indicator SpecTemplate spec needs 200 closed
    candles before it can evaluate. With B.3 having seeded 1H
    ingestion ~2 hours before this test, the cycle was at exactly
    199 → 200 bars during the verification window — the next 1H
    bar close at 17:00 UTC was what unblocked the test. Any future
    1H strategy seeded into a fresh trader environment needs to
    wait at least 200 hours (~8.3 days) of 1H accumulation before
    its first evaluation. Pattern matches Modern Turtle's
    Donchian-driven 255-bar warmup; not new behaviour.
  - **Cleanup.** Single `DELETE FROM trader_strategies WHERE id =
    'd3999c01-...'` cascaded via `ON DELETE CASCADE` on all four
    downstream tables (`trader_strategy_versions`,
    `trader_strategy_state`, `trader_signals`,
    `trader_drift_metrics`). Post-DELETE state confirmed: 3
    strategies, 3 versions, 0 orphaned rows in state / signals /
    drift. The next cycle's `versions=3` log line is the
    end-to-end clean confirmation.
  - **v1 regression: bit-identical.** 3 strategies still
    operating, Modern Turtle warmup unchanged at 218/255,
    heartbeat fresh (28s stale at end), no alerts. trader_worker
    NOT restarted (B.5 was pure runtime observation; no code
    change).
  - **Test count unchanged (1087 passes).** No new automated test
    written — the verification was a one-shot live observation,
    not a permanent test fixture. Adding a recurring "insert
    synthetic version + observe + delete" automated test would
    require coordinating with the live cycle scheduler in CI,
    which the brief explicitly scoped out ("verification, not
    code").

### B.6 — Drift analyzer at higher cadence
- **Ships:** a documented threshold-scaling rule for the drift
  analyzer's "paper-vs-backtest" comparison, since the per-window trade
  count scales with timeframe frequency. Tests on synthetic 1H data
  asserting the scaled thresholds don't false-trigger.
- **Deps:** B.3 (need real-ish 1H trade counts as fixtures).
- **Sessions:** 1.
- **Acceptance:** drift thresholds documented in the design doc + the
  code; synthetic-data tests for 1H + 4H pass; existing 4H drift
  behaviour bit-identical; ruff/pyright green; full suite green.
- **Deferred:** per-symbol drift threshold tuning; per-strategy-style
  threshold tuning (e.g. mean-reversion vs trend-follow).
- **Shipped (2026-05-23, single commit):**
  - **Phase 1 finding:** drift analyzer is a single file
    (`workers/.../trader/drift.py`, ~680 lines including 130 lines of
    load-bearing docstring) with **exactly 5 tunables**:
    `_MIN_PAPER_TRADES=5`, `_HEALTHY_DEVIATION_THRESHOLD=0.30`,
    `_WATCH_DEVIATION_THRESHOLD=0.60`, `_DRAWDOWN_BREACH_RATIO=1.5`,
    `_DEVIATION_EPSILON=0.0001`. Daily cycle (`tick_drift` at 01:00
    UTC), reads `trader_paper_positions` rows, compares against the
    version's stored `backtest_metrics` JSONB, persists a
    `trader_drift_metrics` row + warning alert on breach. No ML
    models, no external services, no fee/slippage coupling — clean
    surface to scale.
  - **Sqrt(N) Brownian scaling** (Q5 resolution from the design
    review, commit `0223b8e`):

    ```
    sqrt_n_scaling_factor(tf) = sqrt(bars_per_day(tf) / bars_per_day(4h))
        4h  → 1.0    (identity; baseline preserved)
        1h  → 2.0    (sqrt(24/6))
        15m → 4.0    (sqrt(96/6))
        1d  → ≈0.408 (sqrt(1/6))
    ```

    Applied as a **multiplier** on the deviation thresholds — a 1H
    strategy gets a 60 % / 120 % / 3.0× band (vs the 4H 30 % / 60 %
    / 1.5×). The intent is to be **conservative on first
    deployment** at a new timeframe: a Brownian-cumulative
    interpretation justifies the widening (cumulative P&L drift
    over a fixed window has std-dev ~sqrt(N)), even though the
    current metrics are per-trade averages where the per-mean
    noise scales the *other* direction (1/sqrt(N)). The
    consequence: drift will under-flag rather than over-flag at 1H
    initially; Phase B.7+ empirical tuning will tighten as needed.
    The trade-off is recorded in the `sqrt_n_scaling_factor`
    docstring so future readers see the rationale.
  - **Scaling table — which tunables scale, which don't:**

    | tunable | scales? | reasoning |
    |---|---|---|
    | `_HEALTHY_DEVIATION_THRESHOLD` | **yes** | Brownian band — widens at higher cadence |
    | `_WATCH_DEVIATION_THRESHOLD` | **yes** | Brownian band — same |
    | `_DRAWDOWN_BREACH_RATIO` | **yes** | drawdown of Brownian motion grows with sqrt(N); same direction |
    | `_MIN_PAPER_TRADES` | no | sample-size floor; 5 trades is statistically thin regardless of how long it took to accumulate |
    | `_DEVIATION_EPSILON` | no | numerical div-by-zero guard, not a threshold |

  - **API additions** (both exported via `__all__`):
    - `sqrt_n_scaling_factor(timeframe: str) -> Decimal` — the raw
      factor. Defaults to 1.0 on unknown timeframes with a warning
      log, so a typo doesn't silently widen / tighten bands.
    - `scaled_thresholds(timeframe: str) -> _ScaledThresholds` —
      returns a frozen dataclass with `(healthy, watch,
      drawdown_breach)`. At `"4h"`, returns the original
      `Final[Decimal]` constants byte-for-byte (the bit-identity
      gate for the existing seeded Phase A strategies).
  - **`_classify_health` signature** now accepts three optional
    threshold kwargs, defaulting to the original module constants.
    Every existing call site that omits the kwargs continues to
    behave exactly as pre-B.6. The orchestrator
    (`compute_and_persist_drift_for_all`) calls
    `scaled_thresholds(primary_tf)` per version and passes them
    through; v2 versions are single-timeframe per Q3 resolution,
    so `version.timeframes[0]` is unambiguous.
  - **`_load_active_versions` extended** to also return the
    version's primary timeframe (third tuple element). Empty
    `timeframes` array (defensive) falls back to 4H baseline.
  - **No schema migration.** `trader_drift_metrics` doesn't
    record the scaling factor used — derivable from the version's
    timeframe on read-back. If future audit needs explicit
    per-row factor recording (e.g. drift behaviour analysis across
    a timeframe-default change), add a `scaling_factor_used`
    numeric column then.
  - **v1 regression: bit-identical.** All 28 pre-existing drift unit
    tests pass without modification (they assert on
    `_classify_health` and `_two_sided_deviation` with the original
    band semantics; the new threshold kwargs default to the
    pre-B.6 constants). The 3 running Phase A strategies are 4H →
    factor 1.0 → thresholds unchanged. trader_worker NOT restarted
    (no runtime path change for currently-running strategies; the
    new code only differs from old when a 1H/15m version exists).
  - **Test count:** 1087 → 1098 (+11 new tests across three
    classes — `TestSqrtNScalingFactor` (5), `TestScaledThresholds`
    (3), `TestClassifyHealthWithScaledThresholds` (3)).
  - **Suite status:** ruff clean, pyright clean, full suite green.
  - **Empirical tuning plan** (Q5 deferred work): when B.7's first
    seeded 1H strategy accumulates enough paper trades to drive
    drift analysis (~5 trades at 1H = ~1 day of observation),
    record the first month of `trader_drift_metrics` health
    classifications. If they cluster at HEALTHY when intuition
    says WATCH (false-negative), or at BREACH when intuition says
    HEALTHY (false-positive), the multiplicative-Brownian factor
    needs adjusting. The current MULTIPLY-by-sqrt-N is biased
    toward false-negatives at 1H; if that's observed, the fix is
    to switch to DIVIDE (use 1/sqrt(N) for per-trade-average
    metrics, keep MULTIPLY for cumulative-equity metrics) — both
    are sensible and the empirical data tells us which.

### B.7 — 1H strategy seedability proof
- **Ships:** end-to-end seed of a real 1H strategy (extracted from a
  source, backtested, gauntlet-passed, seeded via the spec-template
  routing, approved). Validates the parameterised path on something
  real. Strategy choice: a simple 1H RSI mean-reversion or a 1H Donchian
  breakout — both well-known, easy to extract honestly.
- **Deps:** B.1, B.2, B.3, B.4, B.5, B.6 (all the supporting machinery).
- **Sessions:** 1–2.
- **Acceptance:** a 1H strategy ends up `enabled=true,
  approved_for_paper=true` in `trader_strategy_versions`, with the
  trader's `versions_loaded` count incrementing; the strategy fires its
  first signal at the expected 1H boundary; v1 4H strategies
  bit-identical.
- **Deferred:** observation-window validation (lives in B.10).
- **Shipped (2026-05-23, single commit — gauntlet rejection, no
  seed):**
  - **Pipeline end-to-end proven through the gauntlet rejection
    point.** B.7's purpose was integration-test the B.1-B.6
    machinery on a real 1H strategy; the pipeline ran cleanly
    through every step, and the gauntlet correctly refused to
    seed the chosen strategy because it isn't paper-worthy. Same
    pattern as Phase A's RSI-pullback rejection and Supertrend
    rejection — the gauntlet doing its job is the system working.
  - **Pre-warm `/data` 1H cache (Phase 1, ~30 s, one-time):** ran
    `get_market_data('BTC/USDT', '1h', 2020..2026)` inside the
    `worker` container. Produced
    `/data/cache/market/BTC_USDT/1h.parquet` (2.6 MB, 55,888 bars,
    spanning 2020-01-01 → 2026-05-18 23:00 UTC). Confirms the B.4
    finding: host-side `uv run` cannot write to `/data/cache`
    (UID 999 vs the host user); the `docker exec marketmind-worker-1`
    path is the working operator pattern. Production gauntlet
    runs for 1H strategies now skip the ~25 s cold-fetch cost.
  - **Extraction (Phase 2):** raw_text → ingest_raw_text job
    finished in 100 ms; extraction job finished in 35 s; verdict
    `fully_extractable`, cost `$0.113`. Spec parsed cleanly:
    Tier-1 EMA(20)/EMA(50) crossover entry + crossover-below
    condition exit + `trailing_atr(mult=2.0, atr_period=14)` stop
    loss, `primary_timeframe="1h"`, direction `long`,
    `fixed_percent_equity 100%` sizing.
  - **Quirk: `instrument.exchange = "spot"`** (not `"binance"`).
    Falls through `_exchange_key()` to the FeeModel/SlippageModel
    fallback path which returns the same 10 / 5 bps defaults as
    the binance_spot table entry, so backtest math is unaffected.
    Worth a future extraction-prompt audit — most published
    strategies write "Binance" or omit the exchange; "spot" is a
    venue-type, not an exchange. Logged here, not blocking.
  - **api container required a rebuild** to pick up the v1.1
    PSAR additions (`step`, `max_step` fields on
    `IndicatorParams`). The api had been up 21+ hours since before
    the 2026-05-23 v1.1 wave; the worker + trader_worker already
    had the new fields (rebuilt for B.3). Backtest API was 500ing
    on strict-validation `extra_forbidden` errors for `None`-valued
    `step` / `max_step` fields in the LLM-emitted spec. Fix:
    `docker compose up -d --build --no-deps api` (~18 s, container
    recreate clean). General lesson: **after any indicator
    whitelist expansion, all three Python containers (api, worker,
    trader_worker) need to be rebuilt before any extraction that
    uses the new fields can round-trip cleanly.** Worth a
    documentation follow-up in `docs/operations/v1.1-todos.md`.
  - **Backtest (Phase 3):** vbt engine on 55,888 1H bars
    produced 513 trades — within the brief's 100-500 expected
    range, confirms entry triggers fire at 1H cadence. Strategy
    metrics: win-rate 31.2 %, expectancy -0.126 %, sharpe -0.371,
    max drawdown 79.9 %, alpha vs buy-and-hold -10.3 %. Clearly
    not paper-worthy. Engine-only timing: 3-run median **1.99 s**,
    max 2.18 s — **at the B.4 2 s budget** (the full backtest job
    is 29 s including data fetch + benchmark buy-and-hold + author
    claim comparison + persistence; the engine itself is the
    perf-budget target, not the pipeline). Linear scaling vs 4H
    (~0.5 s) within noise.
  - **Overfitting analysis (Phase 4):** ran in 145 s. Composite
    score **56/100**, verdict **`mixed_signals`**. Per-bucket:
    walk_forward degradation_ratio 0.0 (terrible — OOS returns
    collapsed across most windows), monte_carlo p-value 0.24
    (24th percentile, no edge over random), deflated_sharpe
    probability ~0.0 (sharpe is consistent with noise + selection
    bias), parameter_sweep peakiness 0.098 (no parameter-overfit
    smoking gun, the one bucket that didn't flag). **Tier-1 spec
    so the original A.4 weights apply (NOT the B.4-style
    state-aware 0.10/0.50 weights), confirmed by
    `spec_uses_stateful_v2(spec) = False`.**
  - **Phase 5+ — NO SEED.** Per brief: "If overfit/mixed_signals,
    STOP without seeding (gauntlet working correctly)." Pipeline
    proven through the gauntlet rejection point. No
    `trader_strategy_versions` row created; no live-cycle
    integration test for the new version (that's covered by B.5's
    synthetic always-HOLD test; the real signal-firing
    integration ships when a strategy actually clears the gauntlet
    — likely a B.7 follow-up session with a different strategy
    choice).
  - **B.1-B.6 machinery verification — all green end-to-end:**
    - B.1 FeeModel: `commission_for_spec` returned 10 bps via
      fallback (`"spot"` exchange string). Backtest commission
      math correct.
    - B.2 SlippageModel: `slippage_for_spec` returned 5 bps via
      fallback. Backtest slippage math correct.
    - B.3 1H ingestion: `trader_candles WHERE timeframe='1h'`
      grew to 201 rows during the session (B.3 baseline 199 + 2
      from the 17:00 and 18:00 closes). Continuous, no errors.
    - B.4 backtest perf: engine-only 1.99 s median on 55,888
      bars, within 2 s budget. Same code path the fixture-based
      perf test (`test_iterative_perf_1h.py`) exercises in CI.
    - B.5 cycle: trader_worker `versions_loaded=3` throughout
      (no seed → no 4th version). The 3 existing 4H strategies
      kept evaluating at their 4H cadence, identical
      `evaluations=2, holds=2, pair_attempts=3, pair_insufficient
      _history=1` (Modern Turtle warmup 218/255) pattern.
    - B.6 drift scaling: not exercised this session (no new
      version → no new drift row). Code is in place and ready;
      will exercise once a 1H strategy clears the gauntlet.
  - **v1 regression: bit-identical.** 3 strategies still
    operating, 3 versions, Modern Turtle warmup unchanged at
    218/255, no alerts, heartbeat 38 s stale (healthy). trader
    _worker NOT restarted.
  - **Test count unchanged (1098 passes).** No code added beyond
    the api rebuild; pipeline verification was live-cycle
    observation, not test fixtures.
  - **B.7 status: pipeline proven, strategy rejected.** The
    end-to-end "extract → backtest → gauntlet → seed → trade"
    integration test is half-shipped — every step worked except
    the seed gate, which correctly refused. A follow-up with a
    different (likely paper-worthy) 1H strategy would close the
    seed-and-trade leg; the brief explicitly recognised this
    possibility.

### B.8 — 15m extension
- **Ships:** add `"15m"` to `TRADER_TIMEFRAMES`; verify ingestion (per
  B.3); verify backtest performance (per B.4, but tighter — 16× the
  bar count); verify cycle handling (per B.5).
- **Deps:** B.3–B.5 (proves the pattern at 1H first; 15m is the same
  pattern at higher scale).
- **Sessions:** 1.
- **Acceptance:** `(BTC/USDT, 15m)` candles ingest; 6-year 15m backtest
  runs in <30 s (16× 4H baseline; aggressive but realistic for an
  iterative engine on ~210 k bars); cycle evaluates at 15m boundaries;
  ruff/pyright green; full suite green.
- **Deferred:** sub-15m timeframes (1m, 5m) — see §6.
- **Shipped (2026-05-23, two commits — config + fixture+perf+cycle+docs):**
  - **Same pattern as B.3 → B.5 at 4× higher density.** The Phase 1
    finding from B.3 ("the architecture is already TF-agnostic")
    held for the third time — no code change in
    ingestion/signal_engine; only the env var + the perf
    fixture + the design doc.
  - **Phase 1 discovery (container-rebuild scope):**
    - `api` container fields = 14 (PSAR present after the B.7
      rebuild). **No B.8 rebuild needed.**
    - `worker` container is **pre-B.1** (no `fee_model` module).
      For B.8 it only runs `get_market_data()` (B.1-pre code
      unchanged for this path; default-cost specs produce
      identical numbers because `spec.costs.commission_pct=0.001`
      already matches the FeeModel default). **No B.8 rebuild
      needed**, per the brief's "ONLY if needed" clause —
      surfaces as deployment-debt for a future hygiene sweep.
    - `trader_worker` drift.py was **pre-B.6** (676 lines vs
      host 807). The deliberate TRADER_TIMEFRAMES rebuild
      brought it up to current main, including the B.6
      `sqrt_n_scaling_factor` + `scaled_thresholds` helpers (a
      bonus that fixes a latent drift-staleness gap if any 1H or
      15m strategy gets seeded later).
    - Only `trader_worker` got rebuilt for B.8.
  - **Config commit (commit `f57b31f`):** Python default flipped
    from `"4h,1h"` → `"4h,1h,15m"` in three sources
    (`workers/.../trader/config.py`, `docker-compose.yml`,
    `infra/.env.example`). Local `.env` updated by the operator
    (gitignored). `test_trader_settings_defaults_are_loaded`
    assertion updated to `["4h", "1h", "15m"]`.
  - **trader_worker rebuild** (`docker compose up -d --build
    --no-deps trader_worker`, ~3.5 min). Clean boot: idempotent
    bootstrap (`scheduled={}` — all 5 ticks survived in Redis
    across the restart), migration check up-to-date, scheduler
    re-acquired. `TRADER_TIMEFRAMES=4h,1h,15m` confirmed in
    container env; `sqrt_n_scaling_factor("15m")` returns 4.0 as
    expected.
  - **First post-rebuild cycle (2026-05-23 19:24 UTC):**
    `ingest_cycle_complete pairs_attempted=6 candles_inserted=398
    pairs_succeeded=6 backfill_attempts=0 gaps_detected=0` —
    6 pairs (2 symbols × 3 TFs); 398 = 199 BTC 15m + 199 ETH 15m
    + 0 incremental 4H/1H (already had latest). 15m rows span
    2026-05-21 17:30 → 2026-05-23 19:15 UTC (~50 hours,
    consistent with `fetch_recent_ohlcv(limit=200)` minus the
    unclosed bar). 4H + 1H untouched (BTC 4H still 218, BTC 1H
    still 202).
  - **15m perf fixture** committed at
    `tests/fixtures/market/btc_usdt_15m.parquet` — 223,527 bars,
    2020-01-01 → 2026-05-18 23:45 UTC, **9.6 MB**. Fetched via
    `docker exec marketmind-worker-1` (the established UID-999
    workaround from B.4/B.7) in ~92 s (224 pages × ccxt
    rate-limit, one-time cost). Production
    `/data/cache/market/BTC_USDT/15m.parquet` populated as a
    side-effect.
  - **Cross-TF perf measurement (Modern Turtle iterative engine,
    3 warm runs each):**

    | timeframe | bars | trades | min | median | max | scaling vs 4H |
    |---|---|---|---|---|---|---|
    | 4H | 13,985 | 160 | 0.225 | **0.256** | 0.328 | 1× (baseline) |
    | 1H | 55,912 | 665 | 1.063 | **1.090** | — | 4.26× (linear-pred 4×) |
    | 15m | 223,527 | 3,198 | 4.327 | **4.440** | — | 17.34× (linear-pred 16×) |

    Essentially linear scaling, with ~8 % overhead at 16× density
    (within noise). The 15m result is **well under the design's
    `<30 s` acceptance budget** and under the new `test_iterative
    _perf_15m.py` threshold of 8 s.
  - **CI perf test** at
    `workers/tests/test_iterative_perf_15m.py`. Asserts wall-clock
    < 8 s. Threshold reasoning: 8 s = ~1.8× median headroom
    (tighter than the 1H test's 5× because absolute wall-clock is
    longer; flat 5 s headroom would be proportionally smaller).
    Early-warning signal: median creeping above ~6 s.
  - **15m cycle verification** (same B.5 synthetic always-HOLD
    pattern):
    - 2026-05-23 19:30 UTC (first cycle with the new 15m bar):
      `versions=4, evaluations=2, holds=2, pair_attempts=4,
      pair_insufficient_history=2` — the 15m test version was
      attempted but had 199/200 bars (one short, matching the
      `_MIN_WINDOW_BARS` floor finding from B.5).
    - 19:31 (ingest brought 15m count to 200): `evaluations=3,
      holds=3, pair_attempts=4, pair_insufficient_history=1` —
      15m test version evaluated cleanly. Same shape as B.5's 1H
      pattern, exactly one cycle delayed by the same floor.
    - 19:32 (steady state): identical to 19:31.
    - 19:33 (post-DELETE): still `versions=4` (delete propagated
      between cycles).
    - 19:34 (post-propagation): `versions=3, evaluations=2,
      holds=2, pair_attempts=3, pair_insufficient_history=1` —
      **bit-identical pre-insert pattern.**
  - **Cleanup.** Single `DELETE FROM trader_strategies WHERE
    id='727f2dc7-...'` cascaded via `ON DELETE CASCADE` to
    `trader_strategy_versions`, `_state`, `_signals`,
    `_drift_metrics`. Post-DELETE: 3 strategies, 3 versions, 0
    orphan rows in state/signals.
  - **v1 regression: bit-identical.** Modern Turtle warmup stable
    at 218/255 throughout, 4H + 1H candle counts unchanged
    (BTC 4H 218, BTC 1H 202, ETH 4H 218, ETH 1H 202), 3
    strategies still operating, no alerts, heartbeat fresh
    (25 s stale at end).
  - **Test count:** 1098 → 1099 (+1 perf test). Suite green
    (1099 in 89 s), ruff clean, pyright clean.
  - **Three-TF coexistence proven.** 4H + 1H + 15m all ingesting
    simultaneously, signal_engine evaluating each version against
    its declared TF only (intersection gate), no cross-TF
    interference. The architecture's TF-agnostic claim now
    validated at three densities.

### B.9 — 15m regression + drift tuning
- **Ships:** any threshold tuning for the drift analyzer at 15m (per
  B.6's framework, now exercised at higher cadence); a 15m strategy
  seedability proof (mirrors B.7).
- **Deps:** B.6, B.8.
- **Sessions:** 1.
- **Acceptance:** a 15m strategy seeded + approved; drift thresholds
  documented to apply at 15m without false-trigger; v1 4H + 1H
  strategies bit-identical; ruff/pyright green; full suite green.
- **Shipped (2026-05-23, single docs commit — gauntlet rejection,
  no seed):**
  - **Pipeline E2E proven at 15m through the gauntlet rejection
    point.** B.9's purpose was integration-test the B.1–B.8
    machinery on a real 15m strategy; the pipeline ran cleanly
    through every step. The chosen strategy is not paper-worthy
    and the gauntlet correctly refused to seed it. Same pattern
    as B.7's 1H run.
  - **Strategy choice:** Bollinger Band mean reversion with an
    EMA-200 hysteresis trend filter on 15m BTC/USDT — Tier-2
    stateful (regime_state for the EMA-200 regime). Designed to
    plausibly clear the gauntlet at 15m density (mean reversion
    has more empirical edge at lower TFs than trend-following,
    which is what failed in B.7).
  - **Pre-warm:** 15m parquet already populated by B.8
    (`/data/cache/market/BTC_USDT/15m.parquet`, 9.6 MB, 223,527
    bars). No fresh fetch needed.
  - **Phase 2 — extraction:** raw_text → ingest_raw_text 3 s;
    extract_strategy 41 s. Verdict `fully_extractable`, cost
    **$0.123**. Spec parsed cleanly:
    - schema_version 2.0, primary_timeframe `"15m"`, direction
      `long`, position_sizing fixed_percent_equity 100 %.
    - Entry: `AND` of (regime_state: enter `close >= 1.005 *
      EMA(200)`, exit `close <= 0.995 * EMA(200)`) AND
      (crossover: close crosses above `bollinger.lower`).
    - Exits: trailing_atr (mult=2.0, atr_period=14) + condition
      (crossover close above `bollinger.middle`) + condition
      (NOT regime_state — the trend-broke exit).
    - instrument.exchange `"spot"` (same B.7 quirk; falls
      through to FeeModel / SlippageModel fallback returning the
      same 10 / 5 bps).
    - **`spec_uses_stateful_v2 = True`, `condition_uses_tier3 =
      False` → Tier-2 confirmed.**
  - **Phase 3 — backtest:** vbt engine on 223,431 1-minute bars
    (the 6-year 15m window minus one bar at the end) produced
    2,088 trades — within the brief's 500-3000 expected range,
    confirms 15m entry triggers fire. Strategy metrics: win
    35.3 %, expectancy -0.261 %, sharpe **-6.5**, max drawdown
    **99.6 %** (lost essentially all capital across the
    backtest), alpha vs B&H -10.7 %. Decisively not
    paper-worthy. Total backtest job 28 s (pipeline overhead);
    engine compute_seconds 17.3 s — at the upper end of the
    design's per-spec budget at 15m but acceptable.
  - **Phase 4 — overfitting (Tier-2 → A.4 state-aware weights
    applied):**

    | bucket | raw | weight | pts |
    |---|---|---|---|
    | walk_forward (degradation_ratio) | 0.0 | **0.50** | 75.0 |
    | parameter_sweep (peakiness) | 0.0 | 0.25 | 0.0 |
    | monte_carlo (p-value) | 0.80 | **0.10** | 87.5 |
    | deflated_sharpe (probability) | 0.0 | 0.15 | 100.0 |

    **Composite 61.25 / 100, verdict `likely_overfit`.**
    Weights sum to 1.00; walk_forward 0.50 + monte_carlo 0.10
    are the B.4-era state-aware values (vs the Tier-1 base 0.35
    + 0.25). Confirmed correct routing via
    `spec_uses_stateful_v2 = True`.
  - **Walk-forward detail:** 6/6 windows produced negative IS
    and OOS returns. `out_of_sample_positive_rate = 0.0` —
    **zero OOS windows had positive returns.** Both IS and OOS
    averaged negative; degradation_ratio bottomed at 0.0.
    Strategy is broken end-to-end, not "fitted-then-decayed."
  - **Monte-Carlo detail:** 50 permutations. Real return
    -0.996, synthetic mean return -0.994 — the strategy ranks
    marginally WORSE than 80 % of random-shuffled time-series.
    No edge. Note for B.6 empirical-tuning observation: this
    monte_carlo result behaves the same whether the per-trade
    averages are over-fitted or just broken — the test
    correctly flags both.
  - **Deflated Sharpe detail:** observed sharpe -6.51 vs
    expected_max_sharpe 2.53 (after deflation for ~100
    estimated trials). `probability_strategy_is_real = 0.0`,
    `deflated_sharpe_ratio = -9.04`.
  - **Phase 5+ — NO SEED.** Per brief: "If overfit/mixed
    _signals, STOP without seeding — but report the composite
    breakdown so we understand what failed." Strategy isn't
    paper-worthy; pipeline proven through the rejection point.
  - **B.1-B.8 machinery verification — all green end-to-end at
    15m:**
    - B.1 FeeModel: `commission_for_spec` returned 10 bps via
      `"spot"` fallback. Backtest commission math correct.
    - B.2 SlippageModel: 5 bps via fallback. Backtest slippage
      math correct.
    - B.3 15m ingestion: `trader_candles WHERE timeframe='15m'`
      stable at 201 rows during the session (B.8 baseline 199
      + 2 from the 19:30 and 19:45 closes). Continuous, no
      errors.
    - B.4 backtest perf: vbt engine on Tier-2 spec ran in
      compute_seconds 17.3 s including benchmark + persistence;
      the engine-only piece is below the design's 30 s budget
      for 15m. (The iterative-engine 8 s perf-test from B.8
      doesn't apply directly — that's the iterative path on
      Modern Turtle; this is the vbt path on a different spec
      shape — but both are well within design budgets.)
    - B.5 cycle: trader_worker `versions_loaded=3` throughout
      (no seed → no 4th version). Pattern bit-identical.
    - B.6 drift scaling: **state-aware weights for Tier-2
      correctly applied** (the composite contributions show WF
      weight 0.50 not 0.35, MC weight 0.10 not 0.25). The
      sqrt(N) threshold scaling for the daily drift cycle is
      NOT exercised because no version was seeded.
    - B.7 + B.8 container-rebuild discipline: api +
      trader_worker both up-to-date from prior rebuilds; no
      new rebuild needed for B.9.
  - **v1 regression: bit-identical.** 3 strategies still
    operating, 3 versions, Modern Turtle warmup unchanged at
    218/255, no alerts, heartbeat 47 s stale (healthy).
    trader_worker NOT restarted. 4H + 1H + 15m candles
    continuing to accumulate naturally (BTC 4H 218, 1H 202, 15m
    201; ETH same).
  - **Test count unchanged (1099 passes).** No code added —
    B.9's verification was live-pipeline observation.
  - **B.9 status: pipeline E2E proven at 15m; strategy
    rejected (twice in Phase B now, B.7 + B.9).** The two
    rejections are the system working correctly — neither
    strategy was paper-worthy. The pipeline IS proven at both
    1H and 15m; the seed-and-trade integration test for a
    SIGNAL-FIRING strategy at either TF is a separate
    strategy-hunting exercise (not gated to a Phase B
    sub-phase). For B.10's final sign-off, the seed-leg
    machinery is verifiable via the B.5 / B.8 synthetic
    always-HOLD tests (cycle path) and via the existing 3 4H
    strategies in production (full live-execution path).

### B.10 — Phase B final sign-off
- **Ships:** a `docs/operations/phase-b-complete.md` (mirrors
  `phase-a-complete.md`); final regression run; design-doc reality
  reconciliation; CLAUDE.md update (current phase, sacred artifacts,
  Phase B section + Phase B hard-won knowledge).
- **Deps:** B.1–B.9.
- **Sessions:** 1.
- **Acceptance:** all acceptance criteria in §7 met; commit on `main`;
  ready for the next phase to be planned.

**Total session estimate:** ~8–11 sessions. Roughly comparable to
Phase A's footprint, but more uniform (no genuinely "hard" sub-phase
like A.6 was — the architecture is already supportive).

## §4 Open design questions — RESOLVED (2026-05-23)

Six forks were posed in the original design pass. All six are resolved
below; each retains its original framing so the reasoning is recoverable,
and adds a `**Resolved:**` line capturing the locked-in choice. Phase B
implementation proceeds from these as fixed inputs.

### Q1 — Fee data source [was critical]
Live exchange API (periodic refresh) vs static per-tier table (manually
updated). Live: more accurate, more failure modes (API outage degrades
the trader); static: simpler, deliberately stale, requires periodic
manual reconciliation.

**Resolved: static per-tier table.** One table per exchange, default
Binance VIP 0 with current 10 bps; documented quarterly manual refresh
cadence. Revisit only if/when Phase D (live execution) makes
fee-staleness a real risk — paper-only means stale fees produce stale
*paper* numbers, no real harm.

### Q2 — Slippage model abstraction
Stay flat-bps with a per-version override (current behaviour) vs
introduce a `SlippageModel` interface that can express spread + impact.
The flat-bps is fine for 4H; the model abstraction lets 1H/15m have
realistic per-trade slippage without breaking 4H.

**Resolved: introduce the `SlippageModel` interface.** Default
implementation preserves the current flat-bps behaviour exactly
(bit-identical for existing 4H strategies); the interface enables
spread/impact-based slippage for 1H/15m without breaking anything.
Sibling of Q1's `FeeModel` abstraction; ships in B.2.

### Q3 — Strategy timeframe portability [was critical]
Two cleanly-separable options:
- **A. One version per `(symbol, timeframe)`.** Seeding the same
  strategy at 4H and 1H produces two versions. Clean per-version
  semantics, separate backtest/overfitting/drift records per TF.
- **B. One version, multi-TF.** `trader_strategy_versions.timeframes` is
  already `text[]` — technically the same version can drive multi-TF
  evaluation. Cleaner DB; ambiguous semantics if the strategy's spec
  was designed for a specific TF.

**Resolved: A — one version per `(symbol, timeframe)`.** Each TF gets
its own gauntlet / drift / dashboard record. The schema already supports
B (`timeframes: text[]`) but it stays unused for Phase B; each strategy
seed is per-TF. This means "Modern Turtle at 4H" and "Modern Turtle at
1H" are independent experiments with independent results — which is the
correct framing for the discovery question Phase B is actually asking.

### Q4 — Backtest performance target at 15m
Current 6-year 4H Turtle: ~0.355 s. Naive 16× → ~5.7 s. Realistic target
for the iterative engine on 6-year 15m (~210 k bars): **<30 s** for the
backtest, **<5 min** for the full gauntlet (which includes Monte Carlo
re-runs + walk-forward — 16× too). Above that, we vectorise the
iterative engine's hot loop (numba / Cython / numpy-vectorise the inner
T3 dispatch).

**Resolved: <30 s backtest / <5 min full gauntlet at 15m.** If the
iterative engine misses the budget, B.4 (or B.9) ships a vectorisation
of the hot loop. Until then, the current pure-Python iterator is fine.

### Q5 — Drift threshold scaling at higher cadence [was critical]
The drift analyzer compares paper-window trade stats vs the backtest's
expected stats; thresholds are tuned for 4H trade volumes (~6/day). At
1H (~24/day) the same threshold is 4× over-sensitive in trade-count
terms but the per-trade noise is unchanged. Two answers:
- **A. Scale with sqrt(N).** Brownian-motion default — variance of
  sample mean shrinks as `1/sqrt(N)`, so the threshold on the
  *difference* should shrink as `1/sqrt(N)` proportionally. Theoretically
  clean; assumes trades are independent (often not).
- **B. Empirical per-TF tuning.** Run the drift analyzer on synthetic
  paper data at each TF, tune threshold to a documented false-positive
  rate. More work; reflects actual trade dependency.

**Resolved: A as the starting point — sqrt(N) Brownian default.**
Empirical tuning (B) is the natural next step *if* the sqrt(N) default
false-triggers once 1H or 15m runs in practice. This is the exact place
the "observation informs implementation" principle applies — not as a
calendar gate, but as a tightening loop: ship the principled default in
B.6, observe how it behaves on real 1H/15m trade data in B.7 / B.9, and
re-tune in B.10 if reality disagrees with the theory. Document this in
the drift module as the explicit policy.

### Q6 — Per-timeframe fee differentiation
Should the fee model differ per timeframe — e.g., a 15m strategy
implicitly trades more taker fills (less time to wait for maker fills)
than a 4H strategy? OR is fee purely per-(exchange, symbol)? The
honest answer is "it depends on how the strategy is implemented" —
market vs limit orders, hold-time, etc.

**Resolved: fees are per-`(exchange, symbol, order-type)`; order-type
defaults to `taker`.** Not per-TF directly — per-TF effects emerge
through trade frequency × per-trade taker fee. Strategies that
explicitly use limit orders get the maker-fee discount; the pessimist's
default (taker) is what protects backtests from over-claiming.

## §5 Backward compatibility / regression gate

Throughout Phase B, every commit's CI run asserts:

- The three currently-seeded 4H strategies (BB Breakout, Golden Cross,
  Modern Turtle) produce **bit-identical** backtest results
  (signal ledger, fill ledger, P&L, drawdown). Bit-identity is the gate;
  "close enough" is not acceptable. The fee/slippage abstraction
  (B.1/B.2) preserves the current 10 bps default so existing seeded
  versions are unchanged.
- v1 hand-coded templates (`bb_mean_reversion`, `breakout`, `ma_trend`,
  `rsi_mean_reversion`, `vcb`) unchanged.
- The 19-indicator whitelist preserved (Supertrend, ADX, Keltner, PSAR
  remain available; the existing 15 stay).
- Drift parity gates (A.5c prior_signal Turtle; A.6 incremental==one-shot;
  the 2026-05-23 prior_trade gate) zero divergence.
- Daily summary observability continues firing at 00:05 UTC and
  rendering correctly.
- `trader_worker` stable (the 2026-05-23 Redis `health_check_interval`
  fix continues to hold; no idle-timeout restarts).

## §6 Out of scope for Phase B

- **Sub-15m timeframes (1m, 5m, 30m).** Deferred. Their slippage /
  spread / latency dynamics demand a real order-book model the present
  abstraction doesn't deliver. The `observability/queries.py` warmup-ETA
  table is the only place those three are missing; safe to leave until
  someone wants them.
- **Multi-asset (Phase C).** ETH/USDT, SOL/USDT etc. The candle table
  is multi-symbol-capable but the trader currently runs one symbol per
  config; that's a Phase C concern.
- **Live execution (Phase D).** Paper-only assumption is load-bearing
  for every cost-model decision above.
- **New indicators beyond the 19 already in whitelist.** Donchian as a
  first-class indicator is logged in `v1.1-todos.md` and out of Phase B
  scope.
- **Multi-version-per-strategy seeding.** Each (strategy, timeframe)
  combo is its own version (per Q3-A).

## §7 Acceptance criteria for Phase B complete

By the time B.10 signs off:

- `(BTC/USDT, 1h)` and `(BTC/USDT, 15m)` candles ingest cleanly, persist
  in `trader_candles`, and backtest end-to-end through the iterative
  engine within the performance budgets in Q4.
- Fee and slippage models are pluggable; default behaviour preserves
  v1's 10 bps for the existing 4H strategies; per-TF / per-trade-size
  variation is testable.
- At least one 1H strategy and one 15m strategy seeded, approved, and
  running in paper.
- All three drift-parity gates (the existing two plus a new 1H or 15m
  gate if one is added in B.6/B.9) hold at zero divergence.
- All four "live" stateful gates (prior_signal incremental==one-shot,
  prior_signal live==iterative, prior_trade incremental==one-shot,
  prior_trade live==iterative) continue to pass.
- The default-suite test count is ≥1070 + however many new tests B.1–
  B.10 add. No suppressions.
- v1 regression gate green: the three seeded 4H strategies are
  bit-identical to their pre-Phase-B behaviour.
- `pyright` and `ruff` clean throughout.
- `CLAUDE.md` updated (current phase, Phase B section + hard-won
  knowledge mirror of the Phase A pattern).
- `phase-b-complete.md` written, mirroring `phase-a-complete.md`.

## Status

**Design locked (2026-05-23).** §4 open questions all resolved; sub-phase
order in §3 stands as drafted. B.1 (FeeModel abstraction) begins in a
follow-up session. Subsequent sub-phases proceed in the order in §3
with the locked-in answers as fixed inputs.
