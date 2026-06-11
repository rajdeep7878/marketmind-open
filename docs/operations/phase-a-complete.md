# Phase A complete — stateful conditions (2026-05-21)

## TL;DR

Phase A — stateful trading conditions — is **code-complete** on the
`v2-phase-a-stateful-conditions` branch (46 commits, A.1 → A.7). `main` is
untouched and the `trader_worker` container has **not** been rebuilt — both
deliberate. The v2 trader can run Tier-1 (bounded-window), Tier-2
(`regime_state`, `ratchet reset="never"`) and Tier-3 (`prior_trade`,
`prior_signal`, `ratchet reset="per_trade"`) strategy specs, evaluated by
the same engines the backtester uses, with drift-parity gates proving the
live evaluators bit-match the backtest engines they mirror.

Before v2 goes live: an end-to-end review, a one-time `trader_worker`
rebuild, and a migration check on the running DB (see *Operational steps*).
Until a v2 spec is seeded the bot keeps evaluating the existing v1
strategies — v2-capable code, identical v1 behaviour.

## Sub-phase summary

| Sub-phase | What shipped | Commits | Key acceptance test |
|-----------|--------------|--------:|---------------------|
| **A.1** schema | v2.0 stateful condition schema — `ratchet`, `regime_state`, `prior_trade`, `prior_signal`; JSON-Schema descriptions | 2 | `tests/fixtures/strategies/` round-trip + bounds |
| **A.2** extraction | LLM prompt + tool taught the v2.0 stateful conditions and the `prior_trade`/`prior_signal` boundary | 2 | `test_extract.py` stateful-prompt tests |
| **A.3a** T1/T2 engine | Vectorised backtest support for Tier-1 bounded-window + Tier-2 `ratchet`/`regime_state` | 4 | `test_backtest_supertrend.py` (Supertrend regime_state) |
| **A.3b** T3 engine | `iterative.py` Tier-3 simulator + router; `TradeHistory`/`SignalHistory`; the `prior_signal` extension — phantom outcomes (§4.7) | 10 | `test_iterative.py`, `test_backtest_control.py`; Turtle 1→160 trades |
| **A.4** overfitting | State-aware overfitting — continuous-run walk-forward + composite Monte-Carlo re-weight for stateful specs (§5) | 5 | A.4 v1 walk-forward bit-identical regression gate |
| **A.5a** SpecTemplate | `TemplateName.SPEC` + generic `SpecTemplate` executor; migration 0012; seed-script v2 routing (+2 A.5 design commits) | 4 | `test_trader_v1_regression.py`; Supertrend seed acceptance |
| **A.5b** T2 persistence | Migration 0013 `trader_strategy_state`; the idempotency guard; Mechanism A seeded evaluator (§6B) | 5 | `test_stateful_spec_advances_state_exactly_once_per_candle` |
| **A.5c** hardening | Corrupt-state disable-and-alert; exception hardening; Supertrend drift-parity gate; restart recovery (§6B.7) | 4 | `test_drift_parity_supertrend_live_path_matches_backtest` |
| **A.6** live Tier-3 | `iterative_live.py` B3 sibling stepper; `Tier3State` JSONB persistence; live `prior_signal`/`prior_trade` (§6C) | 6 | `test_iterative_live_drift_parity.py` — Turtle, zero divergence |
| **A.7** sign-off | Full-suite + regression-gate verification; this document | 1 | `test_v2_supertrend_smoke_through_signal_engine` |

A.3b's commit count includes the six-commit `prior_signal` extension
(phantom outcomes). Two further commits frame the phase — the kickoff
design doc and one §3.4/§4.3 correction.

## Final test state (A.7 verification, 2026-05-21)

- **Default suite: 959 passed**, 127 deselected, ~66s. `ruff` clean,
  `pyright` 0 errors across `api/src`, `workers/src`, `shared/src`.
- **Integration suite: 124 passed**, 2 skipped, 1 error, ~114s.
  - The 2 skips are expected and environmental: `test_extract.py`
    (`ANTHROPIC_API_KEY` not set), `test_transcribe.py` (audio fixture
    not generated).
  - The 1 error — `tests/test_e2e_dummy_job.py::test_end_to_end_dummy_job`
    — is **not a Phase A regression**: that Phase-0 test hardcodes an
    external Postgres (`postgresql://test:test@localhost:5432/test`) and
    errors at connection time when no such server is running. The test,
    the worker, and the migration runner are all Phase-A-untouched; the
    failure is pre-migration and environmental. Migrations 0012 and 0013
    are exercised and proven by the 124 passing testcontainers tests.

### Regression gates — all green

- **v1 bit-identical:** `test_trader_v1_regression.py` — all 10 cases
  (5 templates × 2 windows) match the pre-Phase-A reference. Structural
  proof: `git diff main..v2-phase-a-stateful-conditions` for the five v1
  template files (`ma_trend`, `breakout`, `rsi_mean_reversion`,
  `bb_mean_reversion`, `vcb`) is **empty** — no v1 template source was
  touched.
- **Drift parity — Supertrend (Tier-2):** `test_drift_parity_supertrend_
  live_path_matches_backtest` — **zero divergence**. The live windowed
  evaluator matches the one-shot backtest.
- **Drift parity — Turtle (Tier-3):** `test_iterative_live_drift_parity.py`
  — **zero divergence** over 1300 bars. The `iterative_live` shadow
  stepper, walked bar-by-bar with `Tier3State` round-tripped through JSON
  each cycle, bit-matches `run_iterative_backtest`.

Both drift-parity gates passing means the v2 sibling/seeded live
evaluators produce bit-identical results to the backtest engines they
mirror — the §6.6 one-evaluator property, verified empirically.

## Three honest flags carried from A.6

1. **Suite timing — resolved.** A.6 observed a 339s default-suite run and
   flagged possible slowness. A.7's clean run is **66s** — the 339s was
   machine load (a long session with competing background tasks), not a
   code regression.
2. **`run_live_cycle` per-cycle cost.** The live Tier-3 stepper rebuilds
   the vectorised condition evaluators over the full candle history each
   cycle — O(history), ~6ms at 1300 bars. This is meaningfully more than
   the iterative engine's amortised ~0.02ms/bar (which builds evaluators
   once per walk), but negligible for a once-a-minute live cycle. Not a
   gate. See design doc §6C and `iterative_live.py`.
3. **Bar-index stability assumption.** The Tier-3 `SignalHistory` is keyed
   on absolute bar indices into the candle history, and `signal_engine`
   loads the full history for a Tier-3 version (`_TIER3_FETCH_BARS`). This
   assumes `trader_candles` is append-only at the end. Historical backfill
   that inserts candles *before* existing rows would shift indices and a
   Tier-3 version's persisted state would need re-derivation. See design
   doc §6C.6.

## Operational steps before v2 goes live

In order:

a. **End-to-end review** of `docs/design/v2-phase-a-stateful-conditions.md`
   and the major implementation files — `iterative.py`, `iterative_live.py`,
   `translator.py`, `signal_engine.py`, `spec_template.py`.
b. **`trader_worker` container rebuild** — one-time; picks up A.5a + A.5b +
   A.5c + A.6 at once. Until this rebuild the running container serves the
   pre-A.5 code (v1 only).
c. **Verify migrations 0012 and 0013 on the running DB.** 0012 widens the
   `trader_strategy_versions.template` CHECK to include `'spec'`; 0013
   creates `trader_strategy_state`. The worker re-applies migrations
   idempotently at startup, so the rebuild in (b) applies them — confirm
   `_schema_migrations` records both.
d. **Bot resumes evaluating v1 strategies under v2-capable code.** No
   behaviour change: v1 strategies are not `SpecTemplate`s, take the
   non-stateful path, and `test_trader_v1_regression.py` proves their
   signals are bit-identical.
e. **(Future) Seed the first v2-native strategy** — Supertrend (Tier-2) or
   Turtle System 1 (Tier-3) — to exercise v2 in paper trading. The seed
   script auto-routes a v2 spec to `template='spec'`.

## Phase A done state

The trader can now run extracted strategy specs **with full fidelity**.
At v1, an extracted strategy had to be force-fitted onto one of five
hand-coded templates — a lossy mapping (BB Breakout → the `breakout`
template, Golden Cross → `ma_trend`), discarding any spec detail the
template could not express. That mapping is **no longer required for v2
strategies**: a v2 spec runs through `SpecTemplate`, which evaluates the
spec itself via the shared backtest evaluators — what the backtest tested
is exactly what the trader runs. The five v1 templates remain, untouched,
for the existing v1 strategies.

Phase A is verification-complete and signed off. Phases B (lower
timeframes), C (multi-asset) and D (live execution) follow.

---

## Rebuild completed — 2026-05-21

The `trader_worker` container was rebuilt from the signed-off
`v2-phase-a-stateful-conditions` branch (`473912a`), putting the v2 code
live in the running bot. The end-to-end review (Phase 1) found no
inconsistency between the design doc and what shipped, and no worrying
code path — see *Concerns* below for two non-blocking observations.

### Timeline (UTC)

| Event | Time |
|-------|------|
| `trader_worker` stopped | 17:37:02 |
| Rebuild started (`docker compose up -d --build`) | 17:37:31 |
| New container started | 17:41:05 |
| Runner startup — migrations applied | 17:41:08 |
| First post-rebuild `signal_cycle_complete` | 17:42:05 |

`trader_worker` downtime ≈ 4 minutes; the other five services (api, web,
worker, postgres, redis) stayed up throughout. A 4h-candle strategy
loses nothing across a 4-minute pause — the next tick re-evaluates the
same open candle.

### Migrations applied

The runner (`marketmind_workers.trader.runner`) applies migrations
idempotently at startup and aborts (`return 1`) if any fails. It applied
0012 and 0013 cleanly (`trader_migrations_applied count=2`). Verified in
`_schema_migrations` (its key column is `filename`, not `version`):

| filename | applied_at |
|----------|------------|
| `0013_v2_trader_strategy_state.sql` | 2026-05-21 17:41:08 |
| `0012_trader_v2_spec_template.sql` | 2026-05-21 17:41:08 |

- **0012** widened the `template` CHECK — it now admits `'spec'`:
  `CHECK (template = ANY (ARRAY['ma_trend','breakout','rsi_mean_reversion','bb_mean_reversion','vcb','spec']))`.
- **0013** created `trader_strategy_state` — schema matches the
  migration: the `(strategy_version_id, symbol, timeframe,
  candle_close_ts)` UNIQUE (the cross-worker idempotency net), the
  `candle_close_ts DESC` "current" index, `state` JSONB,
  `state_schema_version` (default 1), `ON DELETE CASCADE` FK.

Both migrations are additive — v1 strategies continue unmodified.

### v1 regression verification

The first post-rebuild `signal_cycle_complete`:
`versions_loaded=2, evaluations=2, holds=2, signals_persisted=0,
pair_state_disabled=0, pair_state_guarded=0`.

- Both v1 strategies — Bollinger Band Breakout (`breakout` template) and
  Golden Cross (`ma_trend`) — load and evaluate; both HOLD. No spurious
  signals, no drift. (Unit-level proof: `test_trader_v1_regression.py`,
  10/10 bit-identical.)
- `trader_strategy_state` holds **0 rows** — v1 strategies are not
  `SpecTemplate`s, take the non-stateful path, and write no state,
  exactly as designed.
- `trader_worker` logs are clean — no errors, no exceptions. The two
  startup warnings — `trader_orphaned_runs_marked_crashed count=1` and
  RQ's `AbandonedJobError` registry cleanup — are the expected
  consequence of stopping the previous worker, not faults.

### Concerns / notes

- **Not blocking — `worker` Redis timeouts.** The `worker` service (the
  RQ extraction/backtest worker — *not* the trader) was observed
  restarting on a recurring `Redis connection timeout`. It self-recovers
  via `restart: unless-stopped`, is orthogonal to the trader, and did not
  affect the rebuild. Worth a separate look at the `worker`'s Redis
  connection stability.
- **Doc gap — CLAUDE.md.** CLAUDE.md has no Phase A / v2 section and its
  "Current phase" line is stale (Phase 5.2a). It should gain a v2
  capabilities summary and a "Phase A hard-won knowledge" section.
- **Test coverage — live `prior_trade`.** The live drift-parity gate
  exercises `prior_signal` (Turtle). A `prior_trade`-only or
  `ratchet reset="per_trade"` Tier-3 spec reuses the same `run_live_cycle`
  machinery (drift-parity-proven via Turtle) and is backtest-tested
  (A.3b), but has no dedicated live test. Worth adding before the first
  such spec is seeded.

  **Update 2026-05-23 — closed for `prior_trade`.**
  `workers/tests/test_iterative_live_drift_parity_prior_trade.py` adds a
  dedicated drift-parity gate for the `prior_trade` live path —
  EMA(10/30) crossover + `NOT prior_trade(last_won, n=1)` + trailing-ATR
  stop, 1300 bars, bit-for-bit `Tier3State` equality between bar-by-bar
  (with JSON round-trip per cycle) and one-shot, plus a live-vs-iterative
  trade ledger match. The `prior_trade` predicate is structurally sticky
  on this spec (the first trade wins, the gate locks, every subsequent
  EMA-up-cross is gate-blocked) — which is the deliberate exercise: the
  predicate is evaluated on every candidate entry across the 1100+
  post-trade cycles, and `trade_history` is JSON-round-tripped each one.
  `ratchet reset="per_trade"` live remains untested-by-dedicated-gate;
  same architectural confidence (shared `run_live_cycle`), worth adding
  when the first such spec is seriously considered.

**v2 code is live.** `main` untouched; no v2 strategy seeded — the first
v2 seed is deferred to its own session.
