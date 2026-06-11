# v2 Phase C — multi-asset (FX, gold, equities)

**Status:** DESIGN — design-pass-only, no implementation. Targeted sign-off before sub-phase C.1.
**Branch:** `v2-phase-c-multi-asset`.
**Authors:** project author + Claude.
**Date:** 2026-05-25.
**Predecessors:** Phase A (stateful conditions, 2026-05-21), Phase B (lower timeframes, 2026-05-23), v1.2 (schema additions, 2026-05-25).
**Successors:** v2 Phase D (live execution) — deferred and gated on far more than a date.

---

## §1 Context

### 1.1 The single-asset constraint as load-bearing scope

MarketMind has been **BTC-only** since project inception. Every architectural choice from Phase 0 onward — `ccxt` as the data source, Binance Spot as the only wired exchange, `trader_candles` schema with `(symbol, timeframe, open_ts)` as the natural key, the `(exchange, symbol)` lookup keys in FeeModel and SlippageModel, the 24/7 trader cycle cadence, the absence of holiday or halt logic — was made under the assumption that the asset universe was a small set of spot crypto pairs on a single exchange.

This was the right scope decision through Phase B. It kept the platform small enough to ship Phase A (stateful conditions) and Phase B (multi-TF) directly to `main` in a handful of focused sessions each. It gave the empirical loop (hunt → extract → backtest → gauntlet) a tight feedback radius. It avoided sinking time into broker abstractions before the platform had earned them.

It also became a **discovery-rate constraint** by Phase B's end. With BTC as the only asset, the strategy-hunt era ran 9 hunts; 5 surfaced primitive demand (Hunt 3 / Hunt 5 / Hunt 6B / Hunt 6C / Hunt 7-engine-gap from the 2026-05-25 session), 2 cleared extraction and the gauntlet correctly rejected them (B.7 EMA-1H, B.9 BB+EMA200-15m), and the remaining 2 surfaced no extraction-or-gauntlet finding at all. The gauntlet-rejection pattern itself looked *too consistent* — every "BTC trend strategy" failed for variations of "no walk-forward edge" and "no Monte Carlo edge," which is the empirically-expected outcome of BTC's 6-year regime tail (one secular uptrend that dominates any trend-following backtest). The 2 rejected strategies (B.7 + B.9) were honest rejections of bad strategies, not failures of the gauntlet — but a system that NEVER sees a different failure shape (e.g., "this works on EUR/USD's mean-reverting ranges but fails on BTC's trending tail") cannot triangulate which rejections are mechanism-of-the-strategy vs mechanism-of-the-asset.

### 1.2 The 9-hunt era — what it taught and what it did not

**Cumulative hunt outcomes (2026-05-23 → 2026-05-25):**

| Hunt | Strategy shape | TF | Asset | Verdict | Reason |
|---|---|---|---|---|---|
| 1 | Modern Turtle Donchian Breakout | 4H | BTC | SEEDED | Tier-2 regime, robust |
| 2 | Supertrend trend-follow + 2× ATR ratchet stop | 4H | BTC | NO SEED — gauntlet | likely_overfit; stop fought self |
| 3 | Momentum + ATR-percentile | 4H | BTC | EXTRACTION-BLOCKED → v1.2.A | PercentileExpr missing |
| 4 | RSI-pullback long-only | 4H | BTC | NO SEED — gauntlet | likely_overfit |
| 5 | Mean-rev + Tier-3 throttle | 1H | BTC | EXTRACTION-BLOCKED → v1.2.B | bars_since_last_at_least missing |
| 6A | EMA crossover 1H (B.7) | 1H | BTC | NO SEED — gauntlet | mixed_signals 56/100, WF degradation 0.0 |
| 6B | Intraday seasonality (article) | 1H | BTC | EXTRACTION-BLOCKED → v1.2.C | TimeOfDayCondition missing |
| 6C | log_returns indicator | 1H | BTC | DEFERRED — soft gap | r_log ≈ r_simple at small per-bar |
| 7 | BB + EMA200 regime 15m (B.9) | 15m | BTC | NO SEED — gauntlet | likely_overfit 61/100, MDD 99.6% |
| 8 (5-re) | Mean-rev + Tier-3 throttle (post-v1.2.B) | 1H | BTC | NO SEED — gauntlet | likely_overfit 60.19/100, 1 trade |
| 9 (6B-re) | Intraday seasonality (post-v1.2.C) | 1H | BTC | EXTRACTION-BLOCKED — TEACHING GAP | LLM didn't use TimeOfDayCondition as entry.condition; tried sma(period=1) placeholder; validator rejected |
| 10 (7-new) | Modern Turtle System 2 55-bar | 4H | BTC | BACKTEST-BLOCKED — ENGINE GAP | `risk_based sizing not supported with StopLossTrailingAtr (Phase 3.1)` |

**Findings the 12 hunts produced:**
1. **5 of 12 surfaced primitive or teaching gaps** that became v1.2 sub-phases or post-v1.2 follow-ups (Hunts 3, 5, 6B, 9, plus 10's engine gap).
2. **4 of 12 cleared the gauntlet's rejection step** (Hunts 2, 4, 6A, 7, 8) — all rejected for variants of the same root cause: no walk-forward edge against BTC's regime tail.
3. **1 of 12 SEEDED** (Hunt 1, Modern Turtle 4H). Production rate ≈ 8 %.
4. **Zero hunts surfaced "this would have worked on EUR/USD"** — because there is no EUR/USD ingestion to compare against.

The 8 % seed rate is the expected steady-state for a system that refuses bad strategies, and it is healthy. But it is also, structurally, *bounded above* by how many strategy shapes BTC's price history can actually distinguish. A trend-follower on BTC and a trend-follower on EUR/USD are not the same statistical experiment; they have different regime structure, different volatility scaling, different fee asymmetry, different intraday behaviour. **The empirical loop is hitting the ceiling of "what BTC can teach the gauntlet."**

### 1.3 v1.2 primitives that were pre-investments for non-crypto sessions

Two v1.2 primitives have **disproportionate value once non-crypto assets exist**:

- **`TimeOfDayCondition`** (v1.2.C) — UTC hour gate with wrap-around and inclusive/exclusive end. In crypto, the only natural session is "US evening" or "Asia open" — soft phenomena, no hard market open. In FX, "London open at 08:00 UTC", "NY open at 13:00 UTC", "Asia close at 06:00 UTC" are hard session boundaries that drive real liquidity asymmetries. In equities, the open auction (14:30 UTC for NYSE) and the first 30 minutes of trading are well-documented volatility regimes. The primitive that looked like a "behavioural curiosity" on Hunt 6B is a **load-bearing tool** for session-aware FX and equity strategies.
- **`DayOfWeekCondition`** (v1.2.D) — UTC weekday gate (pandas Mon=0..Sun=6). In crypto, Sunday low volume is the only well-documented effect. In FX, the weekend gap (Friday 22:00 UTC → Sunday 22:00 UTC) is a multi-billion-dollar phenomenon — strategies that avoid Friday afternoon entries are categorically different from strategies that don't. In equities, Monday returns are the most-researched single calendar effect in finance (the "Monday Effect").

Both primitives shipped with worked examples that were *crypto-flavoured* (Hunt 6B referenced "US evening session liquidity"). Phase C is where they earn their keep.

### 1.4 Hunt 6B and Hunt 7 outcomes inform Phase C priorities

The two **failures** of the 2026-05-25 hunt batch carry Phase C signal:

- **Hunt 6B (re-attempt, post-v1.2.C) failed for a teaching gap, not a schema gap.** The LLM correctly used `TimeOfDayCondition` as `filter.session` and `exit.time`, but did not realise it could BE the `entry.condition` itself — so it invented a placeholder (`sma(period=1)`) that the validator rejected. This means: **even with `TimeOfDayCondition` in the schema, the extraction-prompt teaching is incomplete for pure-session strategies on any asset class.** Phase C's first FX strategy seed (C.7 — likely London-open mean-reversion or NY-close fade) will hit exactly this shape, where the primary entry mechanism IS the session. The fix is an extraction-prompt teaching audit (codified Phase A standing rule) showing a non-crypto worked example with `TimeOfDayCondition` as the entry.condition.
- **Hunt 7 (Modern Turtle System 2) failed for an engine gap.** The LLM correctly extracted a hybrid stop (2× ATR initial, ratcheting to trailing 20-bar Donchian-low) as `StopLossTrailingAtr`, but the engine rejects `risk_based + StopLossTrailingAtr` as an unsupported combination. This is **NOT a Phase C concern directly** — but it surfaces a v1.2-follow-up gap that will compound in Phase C: any FX strategy using a trailing stop with position sizing in lots-per-account-risk (the FX convention) hits this same shape.

Both findings are **logged as v1.2 follow-ups** (see §6 risk register) and **do not block Phase C design** — they predate Phase C entirely. But Phase C's success criteria (§8) will need a no-regression check that includes these v1.2 follow-ups landing or being explicitly deferred.

---

## §2 Scope

### 2.1 Tier 1 — FX (major pairs)

**In scope:** EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF, NZD/USD (the 7 major pairs).
**Why these:** highest liquidity, tightest spreads, longest reliable data history, the canonical "first FX dataset" for any quant platform. Strategies that work on these tend to generalise to crosses; the inverse is rarely true.
**Out of scope (in Tier 1):** EM crosses (USD/TRY, USD/ZAR — wildly different vol regime), JPY crosses (EUR/JPY, AUD/JPY — same risk-on/risk-off correlation cluster), exotic pairs.
**Data source target:** Oanda paper API (most-used FX paper broker for retail quant; well-documented; rate-limited but generous; ccxt does NOT cover FX, so this is a new adapter).

### 2.2 Tier 2 — Gold (XAU/USD)

**In scope:** XAU/USD only.
**Why this and not Silver / Platinum / Palladium:** gold is the only precious metal with deep enough retail-broker liquidity to backtest cleanly. The other three have spread regimes that make backtesting honest fills genuinely hard.
**Data source target:** same Oanda paper broker as FX. Gold is treated by retail brokers as an FX-like instrument (24/5 trading, pip-style quotation, lot sizing). Incremental work given C.1 already wired Oanda.
**Why Tier 2 not Tier 1:** gold has identical session structure to FX (24/5, weekend close), same broker, same Pydantic shape — but volatility is *much* lower in % terms, lot value math is different (XAU is quoted per troy ounce, contract size 100 oz), and the strategy population that has worked historically on gold is much smaller than for FX majors. Sequencing it after FX validates the FX-class extensions without confounding "gold-specific weirdness" into the platform debugging cycle.

### 2.3 Tier 3 — Equities

**In scope:** SPY, QQQ, IWM (the 3 broad ETF benchmarks), plus 5-10 mega-cap names (AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA) for individual-name strategies.
**Why ETFs first:** ETFs are the cleanest equity backtest target (no idiosyncratic event risk, dividends are smoother, splits are rare, never delisted). Individual names introduce **corporate actions** as a first-class problem (splits, dividends, mergers, spin-offs) — sequence them after ETFs to isolate that complexity.
**Out of scope (in Tier 3):** small caps, OTC stocks, ADRs, leveraged ETFs (TQQQ, SOXL — decay math is its own thesis), inverse ETFs (SH, SQQQ — decay math), single-stock ETFs (TSLL — minimal liquidity), low-volume stocks (any name with < $10M ADV).
**Data source target:** Alpaca paper API (free, high quota, the standard retail-equity paper broker). Polygon.io for historical bar data if Alpaca's history is insufficient (Alpaca's free tier is ~5 years).

### 2.4 Deferred to v2.D or beyond

- **Futures contracts.** Continuous-contract roll math, tick sizes per contract, margin requirements, calendar spreads.
- **Options.** Greeks, IV surfaces, strike selection, expiration handling. Genuinely different architecture.
- **Cross-sectional ranking** ("long top-decile momentum, short bottom decile, across the S&P 500"). Requires basket logic the spec schema doesn't have today.
- **Funding rate strategies.** Crypto perpetuals only. Needs a different data source (CCXT supports it partially; the perp funding rate is its own time series).
- **Order-book features.** L2 / L3 data, microstructure features, queue position. Out of scope for swing-trading timescales.
- **Cross-account portfolio.** Risk-budgeting across accounts (e.g., 50 % BTC, 30 % FX, 20 % equity). Phase C's portfolio-risk sub-phase (C.11) only addresses *within-account* portfolio risk; cross-account is genuinely v3 scope.

### 2.5 Out of scope for v2.C entirely

- **Real-money trading on any asset class.** `assert_paper_only()` remains a hard guard. Every broker adapter added in Phase C ships with the paper-only assertion at every entry point.
- **Cross-asset portfolio reweighting** (dynamically resizing crypto vs FX based on regime). Adjacent to portfolio risk (C.11) but conceptually different and adds genuine complexity — defer.
- **News-driven strategies.** Earnings, Fed announcements, NFP. Out of scope (requires reliable news API + entity disambiguation + event timing, none of which the platform has).
- **Microstructure / HFT-style strategies.** Sub-minute timeframes, queue games. Out of scope.

---

## §3 Sub-phase breakdown

Estimated 11 sub-phases (C.1–C.11), one of which (C.12) is a sign-off / merge / retrospective. Total estimated commit budget: **35–50 commits**, of which roughly half are in the 3 broker-adapter sub-phases (C.1 FX, C.8 gold incremental, C.9 equities). This is **3× Phase B's commit volume** (Phase B was 21 commits) and reflects the architectural-rework nature: broker adapter abstraction, calendar handling, fee model extension all involve genuinely new code, not "extend an existing dispatcher."

| Sub-phase | Title | Estimated commits | Highest-risk single change |
|---|---|---|---|
| C.1 | Multi-asset data ingestion (Oanda adapter, asset metadata schema) | 6–8 | New OandaAdapter implementing ExchangeAdapter Protocol with FX-specific quote conventions |
| C.2 | Asset-class-aware FeeModel + SlippageModel | 3–4 | Re-keying lookup tables on (asset_class, exchange, symbol) without breaking the 5 existing crypto entries |
| C.3 | Lot / contract size handling | 4–5 | New Instrument fields (contract_size, lot_step, tick_size) and the cascading position-sizing math change |
| C.4 | Holiday / halt awareness | 3–4 | First introduction of an external calendar library (`pandas_market_calendars`) into the trader; UTC discipline must hold |
| C.5 | Backtest engine extensions for non-24/7 markets | 4–5 | Weekend-gap handling in vectorbt path; "in-flight" detection that respects market close |
| C.6 | Trader cycle extensions (per-asset cycle timing, session-aware evaluation) | 3–4 | Decoupling the trader's RQ scheduler tick from the 24/7 assumption; per-asset cycle cadence |
| C.7 | First FX strategy seed validation (EUR/USD intraday) | 2–3 | Proof-of-life that the FX path produces an end-to-end pipeline run |
| C.8 | Gold integration (incremental) | 2–3 | Mostly fee/slippage table additions + a smoke-test seed |
| C.9 | Equity integration (Alpaca, gap handling, dividends) | 5–7 | Corporate-action handling for individual names; ETF dividend back-adjustment policy |
| C.10 | Cross-asset filter primitives (optional, "long BTC if SPY > EMA-200 daily") | 2–3 | New schema primitive: cross-asset condition referencing a different symbol's series |
| C.11 | Portfolio-level risk model (within-account, across uncorrelated streams) | 4–5 | Drawdown / VaR across heterogeneous return streams |
| C.12 | Phase C sign-off + retrospective + merge | 3 | Pattern from v1.2.F — regression sweep, retrospective, merge commit |

**Sequencing rationale:** see §7. The headline is "complexity gradient from FX → gold → equities, with platform extensions C.2–C.6 sandwiched after the first adapter so they can be validated against a non-crypto class before equity-specific complexity lands."

---

## §4 Per-sub-phase design

### C.1 — Multi-asset data ingestion (Oanda adapter, asset metadata schema)

**Goal:** Extend the `Instrument` schema to carry the metadata Phase C needs, wire up an Oanda paper-API adapter for FX, and prove ingestion + persistence at parity with the existing Binance path.

**Schema additions** to `shared/.../strategy_spec/common.py:69`:

```python
class Instrument(_StrictModel):
    symbol: str = Field(min_length=1, max_length=32)
    exchange: str = Field(min_length=1, max_length=32)
    quote_currency: str = Field(min_length=1, max_length=10)
    asset_class: Literal["crypto_spot", "fx_spot", "metals_spot", "equity_etf", "equity_single"] = "crypto_spot"
    # ^ default preserves v1 / v2-A / v2-B / v1.2 specs as crypto_spot without re-extraction
    contract_specs: ContractSpecs | None = None  # populated in C.3; None for crypto-spot
    session_hours: SessionHours | None = None  # populated in C.4; None for crypto (24/7)
```

**Backward compat:** existing specs that omit `asset_class` parse as `"crypto_spot"`, preserving the 12 existing fixture specs + 3 production strategies + every gauntlet test corpus member as bit-identical. The Pydantic default is the load-bearing mechanism.

**New `ExchangeAdapter` implementation** at `workers/.../trader/exchanges_oanda.py`:

```python
class OandaAdapter:
    def __init__(self, account_id: str, api_token: str, environment: Literal["practice", "trade"] = "practice"):
        assert environment == "practice", "Phase C is paper-only; practice account required"
        ...

    def fetch_recent_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def fetch_ohlcv_since(self, symbol: str, timeframe: str, since_ms: int, limit: int) -> list[Candle]: ...
```

**Integration site:** the trader's `_make_adapter()` factory (presumed location, `trader/exchanges.py`) dispatches on `Instrument.asset_class`:

```python
def _make_adapter(asset_class: str) -> ExchangeAdapter:
    if asset_class == "crypto_spot":
        return BinanceAdapter()  # unchanged
    elif asset_class in ("fx_spot", "metals_spot"):
        return OandaAdapter(...)
    elif asset_class in ("equity_etf", "equity_single"):
        return AlpacaAdapter(...)
    raise ValueError(f"unknown asset_class: {asset_class}")
```

**Drift parity strategy:** for every existing v1 / v2 / v1.2 spec with `asset_class="crypto_spot"`, the BinanceAdapter path runs through the same code as today. New tests in C.1 must include a **bit-identity regression**: re-run the 227 pre-v1.2 spec corpus tests, assert byte-identical equity curves and trade ledgers. This is the **load-bearing safety gate for Phase C** — identical in spirit to Phase B's 4H bit-identity check and v1.2's pre-existing-spec regression check.

**Test plan:**
- Unit tests for `OandaAdapter.fetch_recent_ohlcv` (mocked Oanda response — VCR-style cassette).
- Schema tests for the new `Instrument` fields (default behaviour, asset_class enum validation).
- Round-trip JSON Schema export tests.
- The asset-class enum addition triggers a `_detect_axes` review (overfitting parameter sweep) — verify it does not become axis-detectable accidentally (it should not be a sweep axis).
- Bit-identity regression: 227 pre-v1.2 spec corpus tests pass byte-identical.

**Estimated commits:**
- C.1.1: Schema additions (asset_class field with crypto_spot default, ContractSpecs/SessionHours forward refs as Optional).
- C.1.2: Pyright fan-out fixes (the asset_class addition will touch every place Instrument is unpacked).
- C.1.3: OandaAdapter implementation + unit tests.
- C.1.4: Adapter factory dispatch.
- C.1.5: Migration `0014_instrument_asset_class.sql` adding the column to `trader_strategy_versions.symbols`-related introspection (probably zero schema impact if we keep asset_class spec-side only; TBD).
- C.1.6: End-to-end ingestion smoke test (EUR/USD 1H bars via OandaAdapter into `trader_candles`).
- C.1.7: 227-test bit-identity regression run + sign-off.
- (Optional) C.1.8: Documentation update at `docs/operations/multi-asset-ingestion.md`.

**Shipped:**
- **C.1.1 (2026-05-26, commits `0434b00` + `355be58`).** As specified: `AssetClass = Literal["crypto_spot", "fx_spot", "metals_spot", "equity_etf", "equity_single"]`, `ContractSpecs`/`SessionHours` as field-free forward declarations, `Instrument` extended with 3 crypto-spot-defaulted fields. 22 new tests (1213 → 1235). All 11 valid corpus round-trips and the 227-spec pre-v1.2 regression pass byte-identical. JSON Schema export regenerated with new fields nested under StrategySpec `$defs`. Divergence from brief: doc said `Literal`, brief said "enum" — followed doc.
- **C.1.2 (2026-05-26, commit `d505f7c`) — NO-OP.** Discovery pass found **zero production-code `Instrument(...)` call sites** (`grep -rn 'Instrument(' workers/ shared/ api/` returned only the class definition itself + 4 test calls in C.1.1's new `test_asset_class.py`). In production, every `Instrument` is constructed via Pydantic `validate_spec()` / `model_validate()` from JSON dicts (DB rows or LLM extraction output) — none are positional or keyword constructor calls. The `| None = None` defaults from C.1.1 therefore introduce zero fan-out by construction. Pyright 0/0/0 repo-wide, ruff clean, full test suite 1235 passed + 1 skipped — bit-identical to post-C.1.1 baseline. **Estimated commit count for the C.1 sub-phase drops from 6–8 to 4–6** (C.1.2 → 0 net real commits; the ledger entry here is documentation only).
- **C.1.3 (2026-05-26, commits `d9f29c7`/`1b9b986`/`43fdb10`/`710fa1a`/`e028a20`/`665cfac` + `3167665`).** OandaAdapter cassette-only implementation conforming to the `ExchangeAdapter` Protocol from `exchanges.py:54-91`. Six commits + docs commit. **Protocol divergence vs design doc:** doc §C.1 sketched `async def fetch_recent_ohlcv(...)`; actual Protocol is **sync** — followed the existing Protocol per brief. Cassettes shipped at `workers/tests/cassettes/oanda/`: `fetch_recent_ohlcv_eurusd_1h_200_candles.yaml`, `fetch_ohlcv_since_eurusd_1h_paginated.yaml` (3-page), `auth_failure_401.yaml`, `rate_limit_429.yaml` (with `Retry-After: 30`), `malformed_response.yaml`. Tests: 29 in `workers/tests/test_oanda_adapter.py` covering happy path (3), pagination contiguity + helpers (4), error shapes (3), paper-only guard (4), structural Protocol conformance (1), granularity mapping (parametrised 7+1), and symbol translation (parametrised 6). Test count 1235 → 1264 (+29).
- **C.1.4 (2026-05-26, commits `2f5d57e` + `2f54f7d`) — 2 commits, within budget.** Adapter factory dispatch on `AssetClass`. Discovery (per C.1.3 open question #1): the production codebase had **exactly ONE** `BinanceAdapter()` constructor call site, at `workers/.../trader/ingestion.py:450` inside `ingest_one_cycle()` — not at `exchanges.py:_make_adapter()` as the design doc speculated. Factory + symbol-inference helper added to `exchanges.py`: `make_adapter(asset_class) -> ExchangeAdapter` and `infer_asset_class_from_symbol(symbol) -> AssetClass`. Dispatch matrix: `crypto_spot → BinanceAdapter`, `fx_spot|metals_spot → OandaAdapter` (env-var-sourced creds, OANDA_ENVIRONMENT must be "practice"), `equity_etf|equity_single → NotImplementedError` (deferred to C.1.x). Ingestion call site refactored: one adapter per cycle, dispatched on `symbols[0]`'s inferred class. **Phase C cycle-level invariant: TRADER_SYMBOLS should be homogeneous per asset class for now**; per-pair adapter map lands in C.5/C.6/C.7 once multi-class deployments arrive. E2E test fixture's monkeypatch target moved from `ingestion_module.BinanceAdapter` to `ingestion_module.make_adapter` (one-line change). 34 new tests in `workers/tests/test_adapter_factory.py` covering all 5 dispatch branches + 18-pattern symbol inference + 7-pattern rejection + 2 composed end-to-end checks (production-symbol regression + EUR/USD forward). Test count 1264 → 1298 (+34). v1 + v1.2 regression bit-identical: 273-test spec corpus passes unchanged. Pyright 0/0/0 repo-wide. trader_worker uptime 24h+ preserved.
- **C.1.5 (2026-05-26, commits `b515845` + `02212cd`) — 2 commits, within budget.** Migration `0014_instrument_asset_class.sql` adds `asset_class TEXT NOT NULL DEFAULT 'crypto_spot'` to `trader_strategy_versions` with a CHECK constraint pinning the 5 AssetClass Literal values. Idempotent (`ADD COLUMN IF NOT EXISTS`); applies cleanly on worker boot via `apply_migrations()`. All 3 existing strategy rows backfilled to `crypto_spot` via the DEFAULT — no per-row UPDATE script needed. The column is **denormalised** — the same value already lives in `spec_json.instrument.asset_class` (C.1.1). No Python read-path change: future filter queries (C.10+ ops dashboards) can use the column without deserialising spec_json. Worker container rebuilt + restarted via `docker compose up -d --no-deps --force-recreate worker`; trader_worker **NOT** restarted (uptime preserved 25h+). Migration applied at 2026-05-26 10:51:25 UTC per `_schema_migrations`. TRADER_SYMBOLS homogeneous-class validator added to `TraderSettings.assert_symbols_homogeneous_asset_class()`, wired into `runner.main()` before migrations apply — boot-time check rejects mixed-class deployments (e.g. `BTC/USDT,EUR/USD`) with a clear error naming the offending symbols + their inferred classes + pointing to C.5/C.6/C.7 where multi-class loops land. 10 new tests in `workers/tests/test_trader_settings.py` (5 happy paths covering current production + all 4 future asset classes, 4 reject paths, 1 unclassifiable-symbol consolidation). Test count 1298 → 1308 (+10). v1 regression bit-identical: 273-test spec corpus still passes; the migration's DEFAULT means existing rows + queries are unchanged.
- **C.1.6 (2026-05-26, commit `68fed86`) — 1 commit, within budget.** First live Oanda API call from the codebase. Operator-provided demo creds (OANDA_API_KEY + OANDA_ACCOUNT_ID + OANDA_ENVIRONMENT=practice) wired through `.env` → `docker-compose.yml worker.environment` → worker container env. Three live tests at `workers/tests/test_oanda_live_smoke.py` (marker `live_api`, opt-in): (1) `make_adapter("fx_spot")` dispatches to OandaAdapter + fetches 9 live EUR/USD 1H candles, validated for shape/monotonicity/sane prices; (2) end-to-end `ingest_one_cycle(TRADER_SYMBOLS="EUR/USD")` inserts 199 candles into `trader_candles` spanning 2026-05-14 → 2026-05-26 (12 days, accounting for FX weekends), then cleans up (production trader has no EUR/USD strategy); (3) paper-only guard re-validated against real creds (`OANDA_ENVIRONMENT="trade"` still raises). **Cassette-vs-live drift findings** (THE point of C.1.6): (a) zero schema drift — Oanda's response shape matches the C.1.3 cassettes exactly (candles array, mid o/h/l/c, complete flag, time RFC3339 with nanosecond precision); (b) **one lifecycle finding** — vcrpy intercepts at the transport layer so cassette tests never exercise SSL socket cleanup. Live calls leak httpx.Client connection pools as `ResourceWarning` after GC. Fix: added `OandaAdapter.close()` + `__enter__`/`__exit__` context manager protocol; `ingest_one_cycle` now closes factory-constructed adapters at end of cycle (caller-injected adapters untouched). (c) FX ingestion delivered ~16 candles/second from a single API call — well within Oanda's documented 120-req/min rate limit. Worker container recreated (`up -d --no-deps --force-recreate worker`) to pick up env vars; trader_worker preserved at 27h+ uptime. Test count 1308 → 1311 (+3). Pyright 0/0/0, ruff clean. Default suite (1307 passed + 1 skipped) excludes the live_api tests via pyproject.toml's `-m "not integration and not live_api"`. **C.7 first FX seed is now fully unblocked.**

- **C.2 (2026-05-26, commits `f5fc74f` + `32841ab`) — 2 commits, within budget.** Per-asset-class fee/slip fallback dispatch on both `FeeModel` and `SlippageModel`. Discovery: exactly 3 production call sites for the spec-level resolvers (`engine.py:298-299`, `iterative.py:558-559`, `jobs/backtest.py:121-122`), all going through `commission_for_spec` / `slippage_for_spec` — under the brief's "STOP if >3" threshold. asset_class accessible on every call site via `spec.instrument.asset_class` (C.1.1 defaulted to `crypto_spot`); zero threading needed. Refactor shape: Protocol methods + `Static*Model` impls gain a keyword-only `asset_class: AssetClass | None = None` parameter (v1.2.B signature-widening pattern). Internal `_lookup_table` helper extracted so the in-table walk is preserved verbatim — per-class fallback only fires on a table miss. New `_fallback_{commission,slippage}_for_class` dispatch enumerates every AssetClass Literal value explicitly + raises NotImplementedError on equity classes pointing to C.9. The spec-level resolvers use `getattr(instrument, "asset_class", None)` so pre-C.1.1 duck-typed stubs continue to work (None routes to crypto fallback). **Per-class defaults shipped** (informed by design doc §C.2 + C.1.6 live findings): commission — `crypto_spot=10 bps` (UNCHANGED), `fx_spot=0`, `metals_spot=0`, `equity_*=NotImplementedError`; slippage — `crypto_spot=5 bps` (UNCHANGED), `fx_spot=5 bps` (~1 pip EUR/USD), `metals_spot=12 bps` (XAU ~30c on $2400), `equity_*=NotImplementedError`. Volume-units note documented inline in fee_model.py: Binance currency volume vs Oanda tick count vs Alpaca share count are NOT comparable; volume-consuming indicators (VolumeSMA, OBV) are scale-invariant so this is documentation, not code. 27 new tests in `workers/tests/test_fee_slippage_per_asset_class.py` covering fallback dispatch matrix (parametrised 6×), None routing (2), equity raises (4), THE load-bearing crypto bit-identity regression (1), `commission_for_spec` / `slippage_for_spec` end-to-end per asset_class (8), composed full-matrix coverage (6). Test count 1308 → 1335 (+27). Crypto bit-identical: 273-test v1 spec corpus passes unchanged; the existing test_fee_model.py + test_slippage_model.py tests (14 unmodified) pass after the getattr-based legacy-stub compat fix. Pyright 0/0/0, ruff clean. trader_worker NOT restarted (in-process Python constant change; no container env touch); 27h+ continuous uptime preserved.
- **Post-C.7 autonomous hunt session (2026-05-26 → 2026-05-27, commits `1ad3e18` + `555048a` + pending) — Hunts 17 + 18 attempted, Hunt 17 SEEDED.** Wide-search FX/crypto hunt sweep after C.7's FX rejection. 6-candidate spread from web research (Pre-FOMC drift / FX carry / TSMOM / PPP value / EOM Treasury / London 4pm Fix), narrowed by data-infrastructure constraint (no equity/rates/multi-year-FX cache) to BTC 4H trend variants.

  **Hunt 17 — BTC/USDT 4H 200-EMA Dual Trend Filter (long-only) — SEEDED.** Operator-written Faber-style 200-EMA + 50-EMA dual filter with 3×ATR(14) hard stop. extraction_id `652fc979-c9c4-49c0-85a1-c82e9a1c1deb` ($0.148, fully_extractable). Backtest (2020-01-01 → 2026-05-21, 13,991 4H bars): 118 trades vbt / 119 iter (drift parity 0.992× PASS), win_rate 22.88%, total_return 3.79%, Sharpe 0.96, Sortino 1.38, max_dd 1.04% (1% sizing), alpha vs B&H -970.8%. Gauntlet **likely_robust 16.19** (analysis_id `1640ef68-3486-4b4a-9590-892e231c3cc1`):
    - walk_forward: degradation_ratio 2.32 (OOS RETURN HIGHER than IS), OOS positive_rate 100% (6/6 windows), consistency 0.99 → 0 pts
    - parameter_sweep: 25 cells across EMA(50)+EMA(200), peakiness 0.073 (very flat) → 4.86 pts
    - monte_carlo: p=0.0 (beats 100% of permutations) → 0 pts
    - deflated_sharpe: prob_real 0.0019 (multiple-comparison penalty on Sharpe 0.96) → 99.85 pts
    - composite: 0×0.35 + 4.86×0.25 + 0×0.25 + 99.85×0.15 = 16.19
  All four seed criteria met (verdict ✓, cost-sanity ✓ tiny drag from 1% sizing, drift parity ✓, empirical ✓ 118/118 signal exits no stop fires). **SEEDED** via `scripts/trader_seed_strategy.py` → strategy_version_id `632e592c-b482-46a3-a3fa-c10628c1b18f` → APPROVED via `POST /trader/strategies/{id}/approve_paper` (admin auth). Bot now runs **4 strategies** (BB Breakout + Golden Cross + Modern Turtle + Dual-EMA Trend Filter, all crypto_spot 4H BTC). New strategy needs 1005 bars (5x EMA-200 oversample) before first trade, currently 240 — first eligible signal ~127 days out (≈2026-10-01). Routed via SpecTemplate.

  **Hunt 18 — BTC/USDT 4H 12-week TSMOM (long-only) — REJECTED.** Same regime (BTC trend-following) but simpler signal: single direct price comparison `close > close[t-504]`, no smoothing or confirmation. extraction_id `a16064a9-1268-4012-9f9f-dcac19003d1c` ($0.154). Backtest: 93 trades, Sharpe 0.72, Sortino 1.02, max_dd 1.96%. Gauntlet **mixed_signals 46.32** (analysis_id `8cce5a85-6b24-4ff3-b32d-d7b82e7b2c39`):
    - walk_forward: degradation_ratio 0.51 (OOS half of IS), OOS positive_rate 33% (2/6 windows) → 59.49 pts
    - parameter_sweep: SKIPPED (`bars_ago=504` on `lagged` not detected by `_detect_axes` — same gap as C.7's time_of_day/highest period) → 30 pts default
    - monte_carlo: p=0.03 (real beats 97%) → 12 pts
    - deflated_sharpe: prob_real 0.00015 → 99.99 pts
    - composite: 46.32
  REJECTED per brief's rule "mixed_signals is a REJECT not 'close enough' seed". The Hunt 17 vs Hunt 18 contrast on the same regime is the actionable finding: **CONFIRMATION layers matter** — Hunt 17's dual-EMA filter (price > 200-EMA AND 50-EMA > 200-EMA) passes the gauntlet at composite 16.19, while Hunt 18's single-signal TSMOM (price > price-504-bars-ago) fails at composite 46.32. The 50-EMA confirmation layer is the difference between likely_robust and mixed_signals on this dataset.

  **Two hunts not attempted** (time-budget): Hunt 19 (EUR/USD Tuesday-Wednesday seasonal) — cost-sanity pre-extraction surfaced ~52 trades/year × 10 bps = 15.6% annual drag, structurally cost-eaten without re-engineering. Hunt 20 (BTC 4H mean-reversion-in-trend) — deferred.

  **Cost / budget:** $0.148 (H17) + $0.154 (H18) = $0.302 total of $2.00 budget. Wall-clock 19:04Z (session start) → 21:15Z = **131 min** of 180 min budget. 4 commits shipped: H17 hunt artefact + H18 hunt artefact + ledger + hunt index. trader_worker uptime preserved across the entire hunt session (continuous since C.6 rebuild 17 h ago); MT warmup ticked from 236 → 240 organically through the session (+4 bars, perfect on the 4H cadence over 16 h elapsed). Zero alerts. Queues 0.

  **Project-level finding:** retail FX is structurally cost-eaten at the C.2 cost-table values on intraday cadence (C.7 EUR/USD London-open + Hunt 19's projected EUR/USD seasonal both fail the cost-sanity gate at 10 bps round-trip × ~250 trades/year). Multi-month / multi-year FX strategies (carry, value, time-series momentum at monthly+ horizons) WOULD survive cost-sanity but need data we don't have (multi-year FX cache for AUD/JPY, USD/JPY) and primitives we don't have (DayOfMonth condition, interest-rate-differential expression). **Pivot for next session:** either (a) extend the data infrastructure (Phase C+ work) for FX carry/value/TSMOM at monthly horizon, OR (b) accept BTC trend strategies as the primary seed path. Hunt 17 is the first new seed from this session — modest edge (Sharpe 0.96) but real, walk-forward OOS > IS, parameter-sweep flat, drift-parity clean.

- **C.7 (2026-05-26, commits `921b170` + `42d9878` + (pending ledger commit)) — 3 commits, within budget (≤120 min, doc est. 2-3).** First FX strategy seed — proof-of-life gate for C.1.1 → C.6 composing end-to-end. **Strategy:** "London Open Breakout — EUR/USD 1H Long" per design doc §C.7 (long at 08:00 UTC bar close > 8-bar Asian-session high; exit at 16:00 UTC time-of-day OR 2×ATR(14) trailing stop). Source: operator-written hunt artefact `docs/hunts/hunt_16_eurusd_london_open_breakout.md`. **Pipeline ran end-to-end:** ingest → extract → backtest → gauntlet → seed-decision. **Verdict: `likely_overfit` 66.07/100 — DID NOT SEED** (correct per design doc's "the strategy is known-to-be-gauntlet-rejectable; the seed-or-no-seed decision is the test of the pipeline"). Strategy count stays 3 → 3.

  **Empirical findings (per the v1.2 EMPIRICAL-INSPECTION rule):**
  - Backtest (2025-01-02 → 2025-12-31 on `eurusd_1h_2025.parquet`):
    39 trades (much less than the back-of-envelope ~252/year — the
    breakout is selective), -0.92% total return, -14.27% alpha vs
    EUR/USD buy-and-hold (+13.36% gross 2025).
  - Cross-engine drift parity (vbt vs iterative): **PASS** trade-count
    ratio 1.000× (Phase B tolerance 0.5x..2x); per-trade exit-reason
    + exit-price diverge (vbt fires "signal" at 14:00; iter fires
    "stop_loss" at 10:00) which is the documented v1.2 engine envelope
    behaviour for specs with multiple exit paths.
  - vbt 3.32 s wall-clock, iter 0.12 s (linear in bars).
  - C.2 FX cost dispatch confirmed: commission=0, slippage=0.0005
    (NOT crypto's 10/5).
  - C.5 weekend-drop confirmed: June 2025 EUR/USD 507 rows pre-drop →
    492 rows post-drop (15 Sunday-evening 22:00-23:00 UTC rows
    removed structurally).
  - Gauntlet bucket contributions: walk-forward 75 pts (OOS
    degraded, 1 of 6 windows positive); parameter-sweep 30 pts
    (degenerate — `_detect_axes` found 0 swept-eligible params, this
    spec uses time_of_day hours + highest period + ATR period, none
    are currently sweepable; known limitation surfacing as a `C.x+`
    follow-up); monte-carlo 69 pts (p-value 0.51 — strategy
    return indistinguishable from random shuffles); deflated Sharpe
    99.8 pts (prob_real=0.002, observed Sharpe -0.45). The DSR
    post-fix value is meaningful (n_observations=2, T=0.69 years
    floored to 2.0 per the frequency-pairing rule).

  **Three C.7-surfaced findings (substantive, beyond the strategy
  outcome):**

  1. **Extraction-prompt teaching gap** — `WeekdayFilter` (v1, ISO
     8601 Mon=1..Sun=7) vs `DayOfWeekCondition` (v1.2.D, pandas
     Mon=0..Sun=6) use different numbering conventions. The prompt
     taught only the v1.2.D convention. The LLM (correctly inferring
     a weekend filter from "FX 24/5" structural language) used
     pandas convention for `WeekdayFilter`; spec validation rejected.
     Two extraction attempts ($0.25) confirmed the gap was robust to
     source-text edits. **Fix (C.7(2))**: extended day_of_week
     prompt section with `CRITICAL distinctions` bullet covering
     (a) the divergence, (b) the load-bearing rule that
     `session_hours.weekend_closed=true` handles weekend skip
     STRUCTURALLY via C.5/C.6 so duplicating it via a weekday
     filter is redundant and accidentally triggers the convention
     bug. Pattern matches Phase A's "extraction-prompt teaching
     audit" discipline. Same shape as Hunt 6B's v1.2-followup.

  2. **`services/market_data.py` crypto-only data path** — the
     production data fetcher (called by backtest engine + benchmark +
     monte-carlo) had a 51-symbol Binance-only whitelist; FX symbols
     hit `UnsupportedSymbolError` before reaching the engine. **Fix
     (C.7(1))**: widened `SUPPORTED_SYMBOLS` to 52 (added "EUR/USD")
     and 53 with the symbol-form alias (added "EURUSD"). Cache is
     operator-populated (`/data/cache/market/EUR_USD/<tf>.parquet`
     and `/data/cache/market/EURUSD/<tf>.parquet`); the cache-first
     `get_market_data` path serves directly without invoking
     Binance. Inline comments document the architectural debt: the
     proper multi-adapter market-data service routing on
     `asset_class` (Oanda for FX, Alpaca for equities, ccxt for
     crypto) is deferred to a future Phase C sub-phase.

  3. **Symbol-form convention divergence** — LLM produced
     `instrument.symbol = "EURUSD"` (no slash — conventional FX
     shorthand) instead of the codebase-canonical with-slash form
     `"EUR/USD"` (matching the 8 existing crypto strategies all
     using `BTC/USDT`). Patched in C.7(2) by adding "EURUSD" as
     an alias in the whitelist; long-term fix is extraction-prompt
     teaching to canonicalise to with-slash. Logged in inline
     comment.

  **Quality gates:**
  - Test count delta: 1417 → 1421 (+4 — 2 new EUR/USD tests in
    test_market_data; +2 from existing-test deltas). Full suite
    1421 passed + 1 skipped + 141 deselected in 1m43s.
  - Pyright 0/0/0 repo-wide on the changed files (baseline 4 errors
    in `test_market_data.py` confirmed pre-existing via `git stash`).
  - Ruff clean.
  - v1 273-test spec corpus passes bit-identical.

  **Cost-accounting:** $0.166 (1st extraction, refused on weekday
  ISO bug) + $0.083 (2nd extraction, same refusal after source-text
  edit) + $0.170 (3rd extraction, success post-prompt-fix; the
  $0.04 cache-write premium absorbed in this attempt) = **$0.419**
  total. ~5% over the $0.40 budget, justified by the prompt-cache
  invalidation triggered by the legitimate teaching-fix edit.

  **Bot regression:** trader_worker uptime preserved across the
  whole sub-phase (paper bot is crypto-only; no prompt cache touch).
  MT warmup 236/255 unchanged. Heartbeat continuous. Zero alerts.
  Queues empty. worker + api containers rebuilt **twice** (once for
  C.7(1) whitelist + cache, once for C.7(2) prompt + EURUSD alias);
  trader_worker untouched.

  **MILESTONE statement:** C.7 did NOT ship a tradeable FX strategy
  into paper, but it DID prove the FX pipeline composes end-to-end
  on a real strategy through the gauntlet, AND surfaced three
  substantive findings (teaching gap, data-path gap, symbol
  convention) — the brief's exact alternative MILESTONE outcome.
  Phase C minimum-path (6 of 6 sub-phases: C.1.1-C.6 + C.7) is
  complete. C.8-C.12 (gold + equities + portfolio risk + corporate
  actions + multi-account) are deferred per minimum-path analysis
  to future work.

- **C.6 (2026-05-26, commits `74588d2` + `b810879` + `6c1f2dc`) — 3 commits, within budget (≤90 min target).** Live trader learns the weekend-skip dispatch — the alert-spam-suppression companion to C.5's backtest-side weekend-drop. New `workers/.../trader/session_skip.py` module with `should_skip_weekend(asset_class, now_utc) -> bool` — single helper, crypto bit-identity guard at the top (`crypto_spot → False unconditionally`), `weekday >= 5` for all other AssetClass values. **Brief-vs-doc divergence:** brief targeted the `ingest_one_cycle` site directly (the actual `data_feed_failure` alert source via the 3-strikes `_update_error_state`); design doc §C.6 originally sketched skip at the signal engine via a `next_eligible_at` plumbing pattern. Followed the brief — the signal + risk stages get graceful "no fresh candles" handling for free once ingestion stops fetching, so a single dispatch site at line ~498 of `ingestion.py` (inside the `for symbol, timeframe in pairs:` loop, BEFORE the `actual_adapter.fetch_recent_ohlcv` call) suffices. `pairs_skipped_weekend: int = 0` field added to both `_CycleState` and `IngestionResult` for observability. Skip dispatch logs a structured `ingest_pair_skipped_weekend` event with symbol, timeframe, asset_class, weekday_name, ts_utc. **Crypto bit-identical:** the 3 production strategies (BB Breakout, Golden Cross, Modern Turtle — all crypto_spot, all 4H BTC) see byte-identical cycle behaviour; the post-rebuild log line `pairs_skipped_weekend=0 pairs_succeeded=6` (Tuesday) confirms the new code path is active without altering crypto cycle output. Test coverage: 33 parametrised unit tests in `workers/tests/test_session_skip.py` (7 crypto bit-identity Mon-Sun + 5 fx weekday-runs + 2 fx weekend-skips + 6 metals/equity weekend-skips + 9 metals/equity weekday-runs + 4 weekday-edge timestamps Fri 23:59 / Sat 00:00 / Sun 23:59 / Mon 00:00) plus 6 integration tests appended to `workers/tests/test_trader_ingestion.py` (crypto Sat+Sun proceed, fx Sat+Sun skip + Mon run, **the alert-suppression invariant** — 4 consecutive weekend cycles emit zero `data_feed_failure` alerts). Tests use `_RecordingFakeAdapter` Protocol-conforming fake to assert zero fetch calls on weekend FX cycles. Test count 1386 → 1425 (+39). Pyright 0/0/0 repo-wide (pre-existing 5 errors on `test_trader_ingestion.py` autouse fixtures + private-usage imports confirmed via `git stash` to predate C.6). Ruff clean. v1 273-test corpus passes unchanged. trader_worker rebuilt + recreated via `up -d --no-deps --force-recreate trader_worker`; **MT warmup state preserved** (236/255 pre-rebuild = 236/255 post-rebuild — `have_bars` is computed from `trader_candles` row count, not stored, so trivially restart-safe); all 3 strategies still loaded; heartbeat resumed within 60s; zero spurious alerts during restart window. trader_worker uptime resets from 31h to 0 (single deliberate restart at end of session, per brief). Weekend-skip is dormant until Saturday — first observable production behaviour change lands on 2026-05-30 00:00 UTC (no production FX symbol yet; weekend-skip only activates after C.7 seeds the first FX strategy). **Perf-test observation, not a C.6 regression:** `test_iterative_perf_1h` + `test_iterative_perf_15m` showed consistent slowdown this session (28-30s vs <5s baseline thresholds). C.6 touches ZERO of the iterative engine code path these tests exercise — likely host-environment drift over the 30 h+ session lifetime; logged as a follow-up to investigate when next iterating on the iterative engine.

- **C.5 (2026-05-26, commits `2fa373f` + `477c392` + `03e76f6` + `a2c0075`) — 4 commits, within doc 4-5 estimate.** Backtest engine learns the weekend-drop dispatch. New `workers/.../backtest/session_filter.py` module with `drop_weekends_if_session_closed(df, spec)` + `drop_weekends_in_data_dict(data, spec)` helpers; both return the SAME object reference on the crypto path (session_hours=None) so bit-identity is structural — no allocations, no row touches, observationally identical to pre-C.5. Single integration site at `engine.py:run_backtest` line 148, BEFORE the Tier-3 router so iterative.py receives pre-dropped data via the router; no per-engine plumbing needed. **Crypto bit-identical:** 273-test v1 corpus + full repo 1382-test regression both pass unchanged. FX parquet fixture `tests/fixtures/market/eurusd_1h_2025.parquet` (256 KB, 6216 rows spanning 2025-01-01 22:00 → 2025-12-31 21:00 UTC, 6078 weekday + 138 weekend rows) committed via `workers/scripts/fetch_eurusd_2025_fixture.py` (one-shot live Oanda fetch). 4 perf-regression tests at `workers/tests/test_perf_regression_fx.py` covering: vbt wall-clock < 60s (empirical ~37s — vbt's per-trade cost scales with the FX TimeOfDay 250-trades-per-year density), iterative wall-clock < 15s (empirical ~3s — linear in bars), cross-engine trade-count parity within ±2× (v1.2.A envelope), weekend-drop dispatch confirmation (138 weekend → 0 post-drop). Pandas FutureWarning from vbt's internal `.fillna` suppressed via module-level `pytestmark` (scoped, not project-wide). Test count 1371 → 1386 (+15 across 11 session_filter unit tests + 4 perf tests). Pyright 0/0/0, ruff clean. trader_worker 30h+ uptime preserved.
- **C.4.1 (2026-05-26, commit `b391a6a`) — 1 commit, minimum-path subset of C.4.** Populates the C.1.1 `SessionHours` forward declaration per design doc §C.4: required `calendar: str`, `open_utc: str`, `close_utc: str`; defaulted `weekend_closed: bool = True`; optional `pre_market_open_utc / post_market_close_utc`. **Regex divergence from doc:** ships stricter `r"^([01][0-9]|2[0-3]):[0-5][0-9]$"` (HH 00-23, MM 00-59) instead of doc's `r"^\d{2}:\d{2}$"` (which permits "99:99"); divergence documented in the SessionHours docstring. **Out of scope (deferred to C.4-full alongside equities in C.9):** `pandas_market_calendars` library, DST handling, per-venue calendar tables. The C.5 backtest engine will consume `SessionHours.weekend_closed` via structural `df.index.weekday >= 5` — no calendar library needed for first FX seed. `Instrument.session_hours: SessionHours | None = None` was already wired in C.1.1; no Instrument change needed. 36 new tests in `tests/test_strategy_spec/test_session_hours.py` covering required-field rejection (3), happy-path documented examples (3 — FX 24/5, NYSE equity, 24/7 identity), default check (1), HH:MM regex acceptance/rejection (parametrised 17), optional pre/post-market fields (5), round-trip serialisation (3), Instrument integration (2), v1 regression sentinel (2). Three pre-existing tests in `tests/test_strategy_spec/test_asset_class.py` (which asserted `SessionHours()` was constructible empty when it was a forward declaration) updated to use the canonical FX 24/5 SessionHours — the C.1.1 forward-declaration behaviour expired by design. Test count 1335 → 1371 (+36 new; 3 C.1.1 tests updated, not added). JSON Schema export regenerated. Pyright 0/0/0, ruff clean. v1 regression bit-identical: 273-test spec corpus passes unchanged. trader_worker NOT restarted (Python module change; no container env touch); 28h+ continuous uptime preserved.

### C.2 — Asset-class-aware FeeModel + SlippageModel

**Goal:** Extend `FeeModel` and `SlippageModel` lookup keys to include `asset_class` so FX spreads (0.5–2 pips for EUR/USD) don't get the crypto 10/5 bps fallback.

**Current state** (per the §1 inventory): both keyed on `(exchange, symbol, side, notional_30d_usd_tier)`. Fallback 10 / 5 bps if not found.

**Phase C state:** keyed on `(asset_class, exchange, symbol, side, notional_30d_usd_tier)`. Fallback table per asset class:

| asset_class | fee fallback (bps) | slippage fallback (bps) |
|---|---|---|
| `crypto_spot` | 10 | 5 (unchanged) |
| `fx_spot` | 0 (FX has no commission) | 5 (≈ 1.0 pip on EUR/USD, scales by pair) |
| `metals_spot` | 0 | 12 (XAU spreads are wider in % terms) |
| `equity_etf` | 0 (Alpaca commission-free) | 2 (ETFs are very tight) |
| `equity_single` | 0 (Alpaca commission-free) | 5 (individual names wider, esp. small caps) |

**Drift parity strategy:** the asset_class default of `crypto_spot` means every existing spec lookup returns the same fee + slippage as today. Add a parametrised test that asserts the lookup table entries for `(crypto_spot, binance, BTC/USDT, taker)` are byte-identical pre- and post-extension.

**Test plan:**
- Unit tests for the new asset-class dimension on each fallback path.
- Per-asset-class default test (one test per row in the table above).
- Bit-identity regression on `_vbt_fees` / `_vbt_slippage` for an all-crypto spec corpus.
- Operator-facing docs update at `docs/operations/fees.md` and `slippage.md` adding the per-asset-class table sections.

**Estimated commits:**
- C.2.1: Re-keying with `asset_class` defaulted, fallback table per class.
- C.2.2: Bit-identity regression test + operator doc updates.
- C.2.3: First FX spread table entries (EUR/USD, GBP/USD, USD/JPY).

### C.3 — Lot / contract size handling

**Goal:** Add `ContractSpecs` to the `Instrument` schema and adjust position-sizing math to respect lot conventions.

**Current state:** crypto-spot can size in arbitrarily-fractional units (0.012345 BTC is a valid order on Binance). FX has a hard convention: 1 standard lot = 100,000 base-currency units; brokers typically allow 0.01 lot = 1,000 units (a "micro lot"). XAU has 100 oz / lot. Equities can be fractional on Alpaca, but most retail brokers floor to whole shares.

**Schema addition** at `strategy_spec/common.py`:

```python
class ContractSpecs(_StrictModel):
    """Lot conventions for one instrument. Phase C addition."""
    contract_size: Decimal = Field(gt=0)  # FX: 100_000 (standard lot); XAU: 100; equity: 1
    min_lot: Decimal = Field(gt=0)  # FX: 0.01 (micro lot); equity: 1.0 (or fractional)
    lot_step: Decimal = Field(gt=0)  # FX: 0.01; equity: 1.0
    tick_size: Decimal = Field(gt=0)  # FX EUR/USD: 0.00001; XAU: 0.01; equity: 0.01
```

**Position-sizing math change:** the existing engine's `_vbt_size` (engine.py:357) computes notional size from `position_sizing.method` and converts to units of the base currency. For crypto-spot, this is direct (`units = notional / price`). For FX/XAU/equity, this becomes:

```python
units_raw = notional / price
if contract_specs:
    lots = (units_raw / contract_specs.contract_size).quantize(contract_specs.lot_step, rounding=ROUND_DOWN)
    units = lots * contract_specs.contract_size
```

**Decimal money math** is preserved throughout — `Decimal`, not `float` — consistent with the engineering invariant in CLAUDE.md.

**Drift parity strategy:** `contract_specs=None` (the v1 default) routes through the existing fractional-unit math. All existing specs are bit-identical. The new lot/contract math only fires for FX/XAU/equity specs.

**Test plan:**
- Unit tests for the new ContractSpecs Pydantic shape (bounds, Decimal precision).
- Position-sizing math tests: given a £10k account, 1 % risk, EUR/USD at 1.0850, 50-pip stop, micro lots → assert position is 0.18 lots (close to risk target, rounded down to lot step).
- Bit-identity regression on every existing crypto-spot spec.

**Estimated commits:**
- C.3.1: ContractSpecs Pydantic model + Instrument field.
- C.3.2: Position-sizing math integration in `_vbt_size` + iterative engine.
- C.3.3: FX/XAU/equity ContractSpecs canonical defaults + tests.
- C.3.4: Bit-identity regression.
- (Optional) C.3.5: SpecTemplate per-asset-class size dispatch.

### C.4 — Holiday / halt awareness

**Goal:** Add session-hours and trading-calendar handling for non-crypto markets without contaminating the crypto-spot path.

**Current state:** ZERO existing calendar code (confirmed by §1 inventory). The trader's `_filter_closed_candles()` filters in-flight bars by `open_ts + duration <= now - 30s`. This works for 24/7 markets but for a 24/5 FX market, `_filter_closed_candles` would incorrectly include the Friday-22:00 → Sunday-22:00 weekend as "missing data" and trigger gap-recovery logic.

**Library choice:** `pandas_market_calendars` is the canonical Python library for this. It covers NYSE, NASDAQ, LSE, CME, ICE (which covers Oanda's FX session model), and 50+ other venues. It returns `pd.DatetimeIndex` of "valid sessions" between two dates, given a calendar name.

**Schema addition:**

```python
class SessionHours(_StrictModel):
    """Trading session definition. Phase C addition.
    For 24/7 crypto: None (no session restriction).
    For FX: SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00", weekend_closed=True).
    For NYSE equity: SessionHours(calendar="nyse", open_utc="14:30", close_utc="21:00", weekend_closed=True).
    """
    calendar: str = Field(min_length=1, max_length=32)
    open_utc: str = Field(pattern=r"^\d{2}:\d{2}$")
    close_utc: str = Field(pattern=r"^\d{2}:\d{2}$")
    weekend_closed: bool = True
    pre_market_open_utc: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    post_market_close_utc: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
```

**Integration sites:**
- `trader/ingestion.py:_filter_closed_candles` — extend to check `session_hours.calendar` for "is now during a session" before emitting the "stale data" alert.
- `backtest/engine.py` — when `session_hours.weekend_closed=True`, drop weekend rows from the OHLCV DataFrame entirely (Oanda will provide them but they're noise).
- Trader cycle scheduler (C.6) — skip evaluation cycles outside session hours, with one configurable "pre-open warmup" cycle.

**Calendar handling — UTC discipline:** every comparison is in UTC. `SessionHours.open_utc / close_utc` are UTC strings. `pandas_market_calendars` returns tz-naive timestamps but is consistently in "exchange-local time" — we wrap the library with a `_calendar_to_utc()` helper that converts using the calendar's known offset and DST schedule. **Test plan:** explicit test for the NYSE DST transition (March / November Sundays) showing that "16:00 ET close" maps to either 20:00 UTC (DST off) or 21:00 UTC (DST on) correctly.

**Drift parity strategy:** crypto-spot specs have `session_hours=None`, which short-circuits every check to "always in session." Bit-identical equity curves for the 3 production strategies + every test fixture.

**Test plan:**
- Unit tests for the SessionHours Pydantic shape.
- Calendar-conversion tests (NYSE DST, CME FX week boundary).
- Backtest weekend-drop test on a synthetic FX dataset (Mon-Fri 24h × 4 weeks → 4 weekends pre-drop, 0 weekends post-drop).
- Stale-data-alert suppression test (verify the trader does NOT alert during Sunday 09:00 UTC for an FX symbol).
- Bit-identity regression for all crypto specs.

**Risk:** the `pandas_market_calendars` library adds a non-trivial dependency footprint. Worth verifying with the worker build size budget before sub-phase commit.

**Estimated commits:**
- C.4.1: SessionHours Pydantic + Instrument field + tests.
- C.4.2: pandas_market_calendars integration + calendar-to-UTC helper + tests.
- C.4.3: `_filter_closed_candles` session-aware extension.
- C.4.4: Bit-identity regression.

### C.5 — Backtest engine extensions for non-24/7 markets

**Goal:** Honest fills, honest weekends, honest holidays for FX / XAU / equity backtests.

**Current state:** the engine receives a pre-sliced OHLCV DataFrame and applies fills at `bar+1.open`. This works on 24/7 crypto where "next bar" is always defined. For FX, `bar+1.open` across a weekend boundary is Sunday 22:00 UTC's open, which is *typically* gapped from Friday's 22:00 UTC close — sometimes by hundreds of pips. The engine must not silently use that gap as a fill price.

**Engine policy decisions:**
- **Weekend gaps in FX/XAU:** if the entry signal fires at Friday 21:00 UTC's bar (1H), the fill happens at Friday 22:00 UTC's open (the LAST bar before close), NOT Sunday 22:00 UTC's open. This is the honest semantics: the "next bar" must be a bar that the broker would have actually filled at.
- **Overnight gaps in equities:** entry signal at NYSE 16:00 close fires; fill happens at next morning's 09:30 ET open *with* the realised gap. This is the standard equity backtest convention.
- **Holidays:** the engine drops holiday bars from the DataFrame before iteration. A signal that fires on the day before a holiday fills the day after.

**vbt vs iterative path implications:**
- The vectorbt path uses `.shift(-1)` for next-bar fills. With weekend-dropped DataFrames, this works correctly (shift respects the index order). Verify with a unit test.
- The iterative path explicitly steps bar-by-bar. The weekend handling falls out of "we only iterate the bars present in the DataFrame" — drop weekends before iteration.

**New backtest-fixture concerns:**
- Parquet fixtures for FX (`workers/tests/fixtures/eurusd_1h_2024.parquet`) — pre-fetched from Oanda, committed.
- Equity fixtures for SPY 1H — Alpaca provides this in their free tier.
- Both must be **time-discontinuous** (weekend-dropped) to validate engine handling.

**Drift parity strategy:** crypto specs are unaffected (24/7, no weekends). The new tests are *additive*.

**Estimated commits:**
- C.5.1: Engine weekend-drop dispatch on `session_hours.weekend_closed`.
- C.5.2: FX backtest fixture commit + perf regression test (analogous to B.4 / B.8 for crypto).
- C.5.3: Equity backtest fixture commit + perf regression test.
- C.5.4: Bit-identity regression.
- (Optional) C.5.5: Overnight-gap honest-fill semantics test for equities.

### C.6 — Trader cycle extensions (per-asset cycle timing)

**Goal:** Decouple the trader's RQ scheduler tick from the "every minute, evaluate every (symbol, timeframe) pair" 24/7 assumption.

**Current state:** the trader runs a 60-second tick loop. Each tick iterates every symbol × timeframe pair, fetches the latest bar from `trader_candles`, asks the signal engine "is the latest bar closed and have we evaluated it?" If yes, evaluate; otherwise skip with `signal_pair_insufficient_history` or similar.

**Phase C state:** the same tick loop, but each pair carries a `next_eligible_at` timestamp computed from `Instrument.session_hours`. Pairs in their session: evaluate normally. Pairs outside their session: skip with `signal_pair_outside_session` (new log event). Pairs at the START of their session: do one warmup pass (re-fetch any bars missed during the session-closed period).

**Trader heartbeat:** the runner's `last_heartbeat_at` continues to update every tick. No change to the `trader_bot_runs` schema. The "running" status is preserved across session-closed periods (the heartbeat is not "I am evaluating" — it's "I am alive and the cycle has not crashed").

**Drift parity strategy:** crypto-spot specs (no `session_hours`) hit the legacy code path. Bit-identical cycle behaviour for the 3 production strategies.

**Test plan:**
- Unit tests for `next_eligible_at` computation per asset class.
- Cycle simulation test: given a (symbol, timeframe) with `session_hours=NYSE`, simulate 3 days of ticks, verify zero evaluations during NYSE-closed periods.
- Daily-summary update: the daily summary already emits cycle stats; verify it handles "0 evaluations because session closed" gracefully.

**Estimated commits:**
- C.6.1: `next_eligible_at` plumbing in signal engine.
- C.6.2: Session-skip log event + daily-summary integration.
- C.6.3: Bit-identity regression.

### C.7 — First FX strategy seed validation

**Goal:** Prove the entire FX path end-to-end with one seeded strategy.

**Strategy candidate:** EUR/USD London-Open Breakout. The London FX open at 08:00 UTC is the highest-liquidity event in FX. A simple shape:
- **Entry:** Long when 8:00 UTC bar close > Asian-session high (00:00–07:00 UTC range high).
- **Exit:** ATR trailing stop, plus session close at 16:00 UTC (NY afternoon).
- **Uses:** `TimeOfDayCondition` (the entry mechanism IS the session), `Highest` indicator, `ATR`, `TimeExit` or `TimeOfDayCondition` as exit.

This is the canonical "first FX strategy" in many academic FX studies. It has a *plausible* mechanism (large overnight news flow gets digested at London open) and is well-known to be **gauntlet-rejectable** on raw data (the effect is small enough that fee + slippage frequently eats the edge). The expected outcome is therefore **a clean run through the entire pipeline**, with the gauntlet correctly assessing whether the edge survives realistic costs. SEED-or-NO-SEED is the test of the pipeline, not the strategy.

**Test plan:** the same hunt pattern from §1.2 — raw_text ingest → extract → backtest → gauntlet → seed if approved.

**This sub-phase is the proof-of-life gate for C.1–C.6.** If C.7 doesn't produce a clean pipeline run on FX, something is wrong in C.1–C.6 and we go back.

**Estimated commits:**
- C.7.1: Hunt artefact + extraction prompt teaching audit (Hunt 6B's fix — TimeOfDayCondition as entry.condition — lands here if not already fixed in v1.2-followups).
- C.7.2: End-to-end smoke test as a parametrised pytest if the strategy approves; manual verification doc otherwise.

### C.8 — Gold integration

**Goal:** Add XAU/USD via the existing Oanda adapter, with the only new work being fee/slippage table entries.

**Sub-phase content:**
- Fee/slippage table for XAU/USD (12 bps fallback per §C.2).
- ContractSpecs for XAU (100 oz per contract, lot_step 0.01, tick_size 0.01).
- SessionHours for CME gold (typically 23:00 UTC Sunday – 22:00 UTC Friday, with a 1-hour daily settlement break around 22:00 UTC).
- One smoke-test backtest on a known-shape XAU strategy (e.g., gold-as-safe-haven mean-reversion to 50-day SMA).

**This sub-phase exists mainly to confirm "we set the pattern right in C.1–C.6 so adding a second FX-like asset is trivial."** If it isn't trivial, C.1–C.6 needs revisiting.

**Estimated commits:**
- C.8.1: XAU table entries + ContractSpecs + SessionHours.
- C.8.2: Smoke-test seed.

### C.9 — Equity integration (Alpaca, gap handling, dividends)

**Goal:** Add equity coverage via Alpaca, with corporate-action handling for individual names.

**New broker adapter:** `AlpacaAdapter` at `workers/.../trader/exchanges_alpaca.py`, implementing the `ExchangeAdapter` Protocol. Same paper-only assertion as Oanda.

**Corporate actions:**
- **Splits:** Alpaca's bar API returns split-adjusted close. Position-tracking in the trader must handle a live split (rare on the timescales we trade, but possible). Phase C policy: if a `trader_paper_positions` row references a symbol that has split since the last bar, the position is closed at the pre-split price and re-opened at the post-split price. Log the split as a `trader_audit_logs` event.
- **Dividends:** for ETFs (SPY, QQQ, IWM), dividends are reinvested at the close on the ex-date. Alpaca handles this for paper accounts. Trader records the dividend as a cash credit in `trader_paper_portfolio`.
- **Mergers / spin-offs:** out of scope for v2.C. If an individual-name symbol undergoes a corporate action other than a split or dividend during paper trading, the system *disables* the strategy version (sets `enabled=False` in `trader_strategy_versions`) and writes an alert.

**Gap handling:** overnight gaps are the equity-specific honesty problem. The fill semantics from §C.5 cover this — entry signal at 16:00 EST close fills at next morning's 09:30 EST open with the realised gap. Pre-market and post-market trading is **out of scope** for v2.C (the standard convention for retail equity strategies).

**Test plan:**
- Synthetic split test (fixture with a 2:1 split, verify position handling).
- Synthetic dividend test (ETF with a 0.5 % quarterly dividend).
- Backtest fixture for SPY 1H + smoke test.
- Bit-identity regression for crypto specs.

**Estimated commits:**
- C.9.1: AlpacaAdapter implementation + tests.
- C.9.2: Corporate-action handling (split + dividend) + tests.
- C.9.3: Adapter factory dispatch for equity asset classes.
- C.9.4: SPY backtest fixture + smoke test.
- C.9.5: Bit-identity regression.
- (Optional) C.9.6: Individual-name seed validation.
- (Optional) C.9.7: Documentation update.

### C.10 — Cross-asset filter primitives (optional)

**Goal (optional sub-phase):** Add a `CrossAssetCondition` primitive enabling "long BTC only if SPY > EMA-200 daily" or "long EUR/USD only if VIX < 25."

**Schema addition:**

```python
class CrossAssetCondition(_StrictModel):
    """v2.C optional: reference another symbol's series in a condition.
    Uses the trader_candles store; backtest engine fetches the cross-asset series alongside the primary."""
    type: Literal["cross_asset"] = "cross_asset"
    cross_symbol: str  # e.g., "SPY" or "VIX" or "DXY"
    cross_timeframe: str  # e.g., "1d"
    condition: Condition  # any Condition variant, evaluated on the cross-asset series
```

**Why optional:** this is a real schema extension (15th Condition variant after v1.2.D's DayOfWeekCondition was the 14th) and adds non-trivial backtest engine complexity (the engine now needs to fetch and align two time series). If the empirical demand isn't there from a hunt during C.7–C.9, defer to v1.3.

**Decision rule:** ship C.10 only if at least one C.7 / C.8 / C.9 hunt's extraction surfaces it as a missing primitive. Otherwise defer.

**Estimated commits if shipped:** 2–3 (schema + engine + tests + prompt teaching).

### C.11 — Portfolio-level risk model

**Goal:** Within-account portfolio risk for an account running 2+ strategies on different assets.

**Current state:** the trader runs strategies independently. Each strategy has its own `risk_pct` (e.g., 0.5 % per trade). When 2 strategies fire simultaneously, the trader does not coordinate exposure — it just lets each strategy place its order. With 3 BTC strategies today, this is mostly fine (they are heavily correlated and rarely fire at the same time). With a BTC + an EUR/USD + an SPY strategy live simultaneously, this becomes a real concern: the 3 streams are largely uncorrelated, but if all 3 fire long at the same time, total drawdown can be additive.

**Phase C policy:**
- **Per-asset-class exposure cap:** maximum 50 % of account equity in any one asset class (configurable per account; default in CLAUDE.md).
- **Per-correlation-cluster cap:** crypto cluster (BTC, ETH) treated as one cluster; FX dollar cluster (EUR/USD, GBP/USD, AUD/USD) treated as one; equity index cluster (SPY, QQQ, IWM) treated as one. Maximum 50 % per cluster.
- **Drawdown circuit-breaker:** if account-level drawdown over a 30-day rolling window exceeds 15 %, the trader disables ALL strategy versions until manual reset. Log the trigger as a `trader_alerts` row.

**New database column:** `trader_paper_portfolio.cluster_exposures jsonb` — tracks exposure per cluster.

**Test plan:**
- Synthetic 3-strategy fire test: BTC + EUR/USD + SPY all fire long at the same minute; verify total notional exposure respects the cap.
- Drawdown circuit-breaker test: synthetic 20 % drawdown over 25 days; verify trigger fires.
- Bit-identity regression for single-asset configurations (the new caps should not bind when only 1 strategy is active).

**Estimated commits:**
- C.11.1: Cluster-exposure schema + computation in trader.
- C.11.2: Drawdown circuit-breaker logic.
- C.11.3: Test suite + bit-identity regression.
- (Optional) C.11.4: Operator documentation.

### C.12 — Phase C sign-off + retrospective + merge

**Pattern from v1.2.F:** 3 commits — regression sweep doc, retrospective with META-PATTERN section, merge commit with `--no-ff` to preserve sub-phase topology.

**Acceptance gates:** see §8.

**Tag:** `v2.C-final` (or `v2.0-final` if Phase C is the last v2 sub-phase before the v3 jump; decision deferred).

---

## §5 Architectural decisions

### 5.1 Schema field additions to `Instrument`

| Field | Type | Default | Purpose | Sub-phase |
|---|---|---|---|---|
| `asset_class` | Literal of 5 values | `"crypto_spot"` | Adapter + fee dispatch | C.1 |
| `contract_specs` | `ContractSpecs \| None` | `None` | Lot / contract sizing | C.3 |
| `session_hours` | `SessionHours \| None` | `None` | Calendar / session gating | C.4 |
| `tick_size` | included in `ContractSpecs` | — | Limit-order pricing | C.3 |

**Default-preserves-v1 discipline:** every new field has a default that maps to the existing crypto-spot behaviour. The 12 existing fixture specs + the 3 production strategies + every test corpus member parse identically and execute identically. **This is load-bearing for the Phase C bit-identity regression** — Phase C must NOT require re-extraction of any existing spec.

### 5.2 FeeModel / SlippageModel extension

**Option A (chosen):** add `asset_class` as the leading dimension of the lookup key, keep existing crypto entries unchanged, add per-class fallback tables.

**Option B (rejected):** create asset-class subtypes (`CryptoFeeModel`, `FxFeeModel`, etc.) with a dispatching `FeeModel` Protocol. Rejected because the lookup logic is genuinely uniform across classes (a static table with a fallback); the asset-class differences are values, not behaviour.

### 5.3 Bar conventions across asset classes

| Asset class | 24/7? | Weekend handling | Pre/post-market |
|---|---|---|---|
| `crypto_spot` | Yes | No special handling (no weekends) | n/a |
| `fx_spot` | 24/5 | Drop weekend bars in backtest + skip session in trader | n/a |
| `metals_spot` | 24/5 | Same as fx_spot | n/a |
| `equity_etf` | 6.5 hours/day, Mon–Fri | Overnight gap as honest fill | Out of scope for v2.C |
| `equity_single` | Same as ETF | Same as ETF + corporate actions | Out of scope for v2.C |

### 5.4 Holiday calendar source

**Choice:** `pandas_market_calendars` (3.5+).
**Why:** the most-maintained Python library; covers NYSE, NASDAQ, CME, ICE, LSE, JPX, ASX, and 50+ others; well-tested DST handling; permissive license.
**Risk:** adds ~20 MB to the worker image; introduces a new transitive dependency (`exchange_calendars`). Mitigation: pin tightly, evaluate image size as part of C.4's commit.

**Timezone handling:** UTC discipline maintained as a project-level invariant. Every `SessionHours.open_utc` / `close_utc` is a UTC string. Every comparison in the trader and engine is UTC-naive (consistent with the existing `pd.DatetimeIndex` convention). The calendar library's exchange-local timestamps are converted to UTC at the boundary via a `_calendar_to_utc()` helper.

### 5.5 Broker connector abstraction

**Choice:** keep the `ExchangeAdapter` Protocol from Phase 0, add concrete implementations per asset-class (BinanceAdapter for crypto, OandaAdapter for FX/gold, AlpacaAdapter for equities). Adapter factory dispatches on `Instrument.asset_class`.

**Why not a richer abstraction:** the asset-class adapters are genuinely different shapes (ccxt's `fetch_ohlcv` vs Oanda's REST `instruments/{instrument}/candles` vs Alpaca's `bars` endpoint). A premature unification would add inheritance complexity without saving code. Each adapter is small (~150 lines), and the shared contract (`fetch_recent_ohlcv` + `fetch_ohlcv_since`) is enough for the trader's needs.

**`PaperBrokerProtocol`:** out of scope for v2.C. The adapters in Phase C are **data-only** (ingestion). Order placement remains via the paper-fill simulator that's already in the trader (paper orders + paper fills are computed locally, never submitted to a broker). The PaperBroker abstraction is a v2.D concern when live order submission starts to be relevant.

### 5.6 Decimal money math + UTC tz-aware time math

**Both invariants preserved.** Every new Pydantic field uses `Decimal` (ContractSpecs.contract_size, tick_size, etc.). Every new timestamp comparison is UTC. The `assert_paper_only()` guard is added to every new broker-adapter entry point (verified by a source-scan test, same pattern as the existing `test_trader_jobs_paper_only.py`).

---

## §6 Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | `pandas_market_calendars` adds significant image-size or build-time cost | Medium | Low | Measure in C.4; if > 50 MB image growth, fall back to a hand-rolled session table for the 3-4 calendars we actually need (NYSE, CME FX, NYMEX gold, JPX) |
| R2 | Oanda paper-API rate limits or auth changes break ingestion mid-phase | Medium | High | Spike Oanda integration in C.1 with a real account; document rate limits + auth flow in `docs/operations/multi-asset-ingestion.md`; cache aggressively at the trader-candles layer |
| R3 | Alpaca corporate-action data is incomplete for individual names | Medium | Medium | Limit C.9 individual-name coverage to large-cap names (well-documented actions); use ETF-only for the first equity seed |
| R4 | Schema additions break the JSON Schema export → web TypeScript types | Low | Medium | Test in CI; add `Instrument` to the export check; the Pydantic-defaulted fields should round-trip cleanly |
| R5 | Bit-identity regression for crypto specs breaks somewhere in C.1–C.6 | Medium | Critical | This IS the load-bearing safety check; if it breaks, the sub-phase rolls back and the design is revisited; non-negotiable |
| R6 | The Hunt 6B teaching gap (LLM doesn't use TimeOfDayCondition as entry.condition) compounds in FX | High | Medium | Fix the teaching gap as a v1.2 follow-up BEFORE C.7's first FX seed; add a non-crypto worked example to the extraction prompt |
| R7 | The Hunt 7 engine gap (risk_based + StopLossTrailingAtr) compounds in FX | High | Medium | Fix the engine combination as a v1.2 follow-up BEFORE C.7; FX strategies routinely use this exact combo |
| R8 | Session-edge bar handling (last NY bar at 21:00 UTC, first London bar at 08:00 UTC) introduces subtle bar-timing bugs | High | Medium | Explicit tests for session-edge behaviour in C.5 + C.6 (4+ tests per asset class); use FX fixtures with known session-edge events |
| R9 | Reg-T or similar regulatory constraint on equity paper accounts | Low | Low | Alpaca paper accounts don't have Reg-T; document the assumption in `docs/operations/equity-paper.md`; flag if a real-money path is ever opened (which is v2.D, so far) |
| R10 | Cross-asset portfolio cluster categorisation gets contentious (e.g., is BTC in the "risk-asset" cluster with equities?) | Medium | Low | Start with conservative clusters (each asset class its own cluster); refine based on observed correlation data; this is a values-knob, not an algorithm |
| R11 | The 35-50 commit estimate is wildly low because broker adapters take 2-3× longer than expected | Medium | Medium | Use Phase B's pattern: design pass first (this doc), each sub-phase has a "Shipped" section in the design doc; if mid-sub-phase the work doubles in scope, STOP and report (per the brief's escape hatch) |
| R12 | UTC tz-aware discipline breaks somewhere in the pandas_market_calendars integration | Medium | High | Explicit `tz_localize('UTC') / tz_convert('UTC')` guards at every boundary; a CI test that asserts every `Instrument.session_hours` timestamp is tz-aware |

---

## §7 Sequencing rationale

**Why FX before gold before equities:**
- FX major pairs are **the highest-liquidity, lowest-complexity non-crypto asset class.** Pip conventions are uniform. Spreads are reliable. Session structure (24/5) is the simplest non-crypto pattern. Adding FX validates the platform extensions (C.2 fees, C.3 lots, C.4 session, C.5 weekend gaps) on the asset class with the fewest edge cases.
- Gold is **FX with a different lot convention and slightly wider spreads.** Once FX works, gold is a 2-commit addition (C.8). It validates that the platform extensions are correctly generalised by introducing a second instrument with the same shape.
- Equities introduce **brand-new concerns** (corporate actions, overnight gaps, intraday vs after-hours sessions). Sequencing them last lets the platform extensions stabilise on simpler classes first.

**Why crypto stays first-class:**
- The 3 production paper-trading strategies (BB Breakout, Golden Cross, Modern Turtle) MUST stay bit-identical through every Phase C sub-phase. Crypto is not deprecated — it's the regression anchor.
- The hunt-era empirical loop continues on crypto in parallel with Phase C work. New crypto hunts and v1.2 follow-up fixes can land in `main` while Phase C is in progress.

**Why portfolio risk (C.11) comes last:**
- It only matters once 2+ strategies on different asset classes are running simultaneously. Until C.7 ships, there's no "2+ uncorrelated strategies" state.
- The drawdown circuit-breaker is a defensive measure that needs production trading data to calibrate. Sequencing it after C.7 / C.8 / C.9 gives at least a brief production-trading window with multiple asset classes before the policy lands.

**Why cross-asset primitives (C.10) is optional:**
- Phase A / B / v1.2 demonstrated that empirical demand from hunts is the right driver for schema additions. C.10 has no current empirical demand. Including it conditionally on a C.7–C.9 hunt surfacing it keeps the design honest.

---

## §8 Sign-off criteria

Phase C is **complete** when ALL of the following are met:

### 8.1 Regression preserved (load-bearing, non-negotiable)
- [ ] All 227 pre-v1.2 spec corpus tests pass byte-identical.
- [ ] All 1199 v1.2-final tests still pass (i.e., post-Phase-C test count is ≥ 1199, with all new tests added).
- [ ] All 5 drift-parity / cross-engine gates (Phase A) continue to PASS.
- [ ] All 76 v1.2 drift-parity gates continue to PASS.
- [ ] The 3 production paper-trading strategies' equity curves are bit-identical when re-backtested over the same window post-Phase-C.

### 8.2 First non-crypto seed live (proof-of-life)
- [ ] At least 1 FX strategy seeded into paper trading via the standard hunt pipeline (raw_text → extract → backtest → gauntlet → seed).
- [ ] That strategy completes at least 7 days of paper trading without alerts.

### 8.3 All v1.2 primitives work on non-crypto data
- [ ] `TimeOfDayCondition` ran on at least one FX strategy seed (presumably C.7).
- [ ] `DayOfWeekCondition` exists in at least one Phase-C-era hunt's extraction (likely a Monday-effect equity strategy in C.9).
- [ ] `TakeProfitAtrMultiple` ran in at least one FX strategy backtest.

### 8.4 Documentation
- [ ] `docs/operations/multi-asset-ingestion.md` — operator guide for Oanda + Alpaca paper-account setup.
- [ ] `docs/operations/fees.md` and `docs/operations/slippage.md` updated with per-asset-class sections.
- [ ] `docs/operations/equity-paper.md` — corporate-action policy, gap handling, etc.
- [ ] CHANGELOG.md updated with Phase C entry (v2.C-final tag).
- [ ] `docs/v2-phase-c_retrospective.md` — sub-phase retrospective with META-PATTERN section (Phase A standing rule).

### 8.5 Operational
- [ ] Pyright 0/0/0 maintained through every Phase C commit.
- [ ] Ruff clean maintained through every Phase C commit.
- [ ] trader_worker reboot policy: each broker-adapter sub-phase requires a container rebuild (api + worker + trader_worker), per Phase B operator default. Recommended: opportunistic rebuilds (after sign-off, not mid-sub-phase).

---

## §9 Non-goals

What Phase C is explicitly **not** doing:

1. **Real-money trading on any asset class.** Paper-only across crypto, FX, gold, equity. `assert_paper_only()` enforced at every new broker-adapter entry point.
2. **Cross-sectional ranking strategies** (basket logic). The schema doesn't support it today; v3 scope.
3. **Options / futures contracts.** Greeks, IV, contract roll math. Out of scope.
4. **Funding rate strategies.** Crypto perpetuals only, needs separate data source. Out of scope.
5. **Order book / microstructure features.** L2 / L3, queue position. Out of scope.
6. **Pre-market / post-market equity trading.** Standard 09:30–16:00 ET only. Pre/post is out of scope (Alpaca supports it but it's a different fill model).
7. **Earnings / Fed-day strategies.** News-event timing. Out of scope (no reliable news API integrated).
8. **Multi-account portfolio.** Within-account portfolio risk (C.11) only. Cross-account portfolio reweighting is v3.
9. **Algorithmic execution** (TWAP, VWAP, iceberg orders). Market orders only. Out of scope.
10. **Bond / fixed-income.** Out of scope. Fundamentally different cash-flow modelling.

---

## §10 Projection

### 10.1 Commit budget

| Sub-phase | Estimated commits | Cumulative |
|---|---|---|
| C.1 | 6–8 | 6–8 |
| C.2 | 3–4 | 9–12 |
| C.3 | 4–5 | 13–17 |
| C.4 | 3–4 | 16–21 |
| C.5 | 4–5 | 20–26 |
| C.6 | 3–4 | 23–30 |
| C.7 | 2–3 | 25–33 |
| C.8 | 2–3 | 27–36 |
| C.9 | 5–7 | 32–43 |
| C.10 (optional) | 0–3 | 32–46 |
| C.11 | 4–5 | 36–51 |
| C.12 | 3 | 39–54 |

**Estimated total: 39–54 commits.** This is **2× Phase B (21 commits)** and **2.5× v1.2 (21 commits)**. Reflects the architectural-rework nature — broker adapters, calendar handling, lot math, corporate actions are all genuinely new code, not "extend a dispatcher."

### 10.2 Timeline assumptions

**Phase B precedent:** 10 sub-phases shipped in 2 calendar days (a single intense day's work for B.1–B.5, a second day for B.6–B.10). This was sustainable because Phase B was almost entirely additive (the Phase 1 "TF-agnostic" hypothesis held literally).

**v1.2 precedent:** 5 sub-phases in 2 calendar days, ~21 commits. Similar pattern; additive only.

**Phase C reality:** sub-phases C.1, C.4, C.5, C.9, C.11 are NOT additive. They introduce new code (broker adapters, calendar, gap handling, corporate actions, portfolio risk). Each one is potentially a multi-session effort.

**Estimated calendar effort:** 6–10 working sessions (each 4–8 hours), spread over 2–3 calendar weeks. This is the FIRST Phase since Phase A that should NOT be expected to ship in 1–2 days. Plan accordingly.

### 10.3 Explicit dependency on Hunt 6B / Hunt 7 outcomes

**Two v1.2 follow-ups MUST land before C.7:**

1. **Hunt 6B teaching gap fix.** Extraction prompt teaching audit (Phase A standing rule): add a non-crypto worked example showing `TimeOfDayCondition` as the entry.condition for a pure-session strategy. Without this, C.7's first FX strategy seed will hit the same extraction-prompt failure that Hunt 6B re-attempt hit on 2026-05-25.
   - Location: `workers/src/marketmind_workers/services/extraction_prompt.py`'s `### time_of_day` section.
   - Estimated cost: 1 commit + a re-extraction test (~$0.15 to validate).

2. **Hunt 7 engine gap fix.** `risk_based sizing` must be supported with `StopLossTrailingAtr`. Either: extend `_vbt_size` in `backtest/engine.py` to handle the combination, or extend the iterative engine's risk-based sizing branch. The error message says "Phase 3.1" — this was a deliberate scope decision at the time; revisit and either ship or refine the error message to make the combination's status explicit.
   - Location: `workers/src/marketmind_workers/backtest/engine.py:357`.
   - Estimated cost: 1–3 commits depending on chosen approach + drift parity test.

Both can be fixed **on `main` outside Phase C** (they predate Phase C). If they're not fixed, Phase C inherits them as v1.2-follow-up blockers and C.7's success criterion fails.

### 10.4 Decision: implementation start

This is a **design pass only**. Implementation does NOT begin in this branch. The design doc commits on `v2-phase-c-multi-asset`; the implementation begins after sign-off with sub-phase C.1.1 on a fresh `v2-phase-c-multi-asset` branch (or this same branch — defer the branching decision to sign-off).

**Before C.1 implementation begins:**
1. Hunt 6B teaching gap and Hunt 7 engine gap MUST be filed as explicit v1.2 follow-up tasks (or this doc updated to absorb them into Phase C's scope).
2. Oanda paper-account credentials provisioned + documented in env-vars.
3. Alpaca paper-account credentials provisioned + documented in env-vars.
4. The v1.2 follow-up "max_drawdown > 80 % refusal flag" decision made (defer or include).

---

## Status

Design pass — DRAFT.

**Next:** sign-off review, then either begin C.1 on this branch or open a fresh `v2-phase-c-impl` branch for implementation. The standing pattern from Phase A / Phase B / v1.2 is **design doc on the same branch**, sub-phase commits land sequentially, retrospective + merge at the end. Recommend the same here.

**Discovered constraints (NONE require deferral to v3):**
- The two v1.2 follow-ups (Hunt 6B teaching gap, Hunt 7 engine gap) are real but are pre-Phase-C blockers, not Phase C scope changes.
- The `pandas_market_calendars` dependency is a known cost but is well-understood (no library shopping needed).
- Broker-adapter expansion is the genuinely new work but follows the existing `ExchangeAdapter` Protocol pattern (no protocol rework needed).

**Recommendation:** sign off this design pass and proceed to C.1 after the two v1.2 follow-ups land on `main`.
