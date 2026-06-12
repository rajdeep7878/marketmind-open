# MarketMind — Project Log (historical research record)

> **Note for public-repo readers:** this is the verbatim engineering log of
> the private research project this repo was curated from — phase
> narratives, hard-won lessons, and the reasoning behind the engine's
> rules. It is published as a research record, so it reads as the working
> notes it was; internal phase names and dates are kept as written. For
> the polished entry points, start with the README and
> `docs/ftr/REPORT.md`.

---

## Current phase / completion narrative (v1.2)

**v1.2 schema additions — COMPLETE, deployed, running.** Sign-off 2026-05-25 (`docs/v1.2_retrospective.md`); merged to `main` at `913994a` and tagged `v1.2.0-final`. Five new schema primitives shipped across 5 sub-phases (A-E) plus baseline + sign-off (7 total, 21 commits): **PercentileExpr** (v1.2.A — new Expression variant, rolling empirical percentile of an inner expression over a trailing window), **prior_trade(predicate="bars_since_last_at_least")** (v1.2.B — 5th PriorTradeCondition predicate, time-based re-entry throttle), **TimeOfDayCondition** (v1.2.C — 13th Condition variant, UTC hour-of-day gate with wrap-around + inclusive/exclusive end), **DayOfWeekCondition** (v1.2.D — 14th Condition variant, UTC weekday gate, pandas Mon=0..Sun=6), **TakeProfitAtrMultiple** (v1.2.E — 4th TakeProfitMethod variant, mult × ATR symmetric to StopLossAtrMultiple; vbt handles SHORT via direction="shortonly"). Each surfaced by real-strategy extractions in the post-Phase-B 9-hunt era (Hunt 3 / Hunt 5 / Hunt 6B). Three Python containers rebuilt at sign-off (api + worker + trader_worker, 4 min back-to-back with `--no-deps`), MT warmup preserved across restart. Total suite 1099 → **1199** (+100); 5 → **76** drift parity gates (+71 cases), zero divergence; perf-regression 1H + 15m PASSED; ruff + pyright clean continuously across all 21 v1.2 commits; 227 pre-v1.2 spec corpus tests bit-identical.

**Phase B (lower timeframes)** stayed the baseline of all v1.2 work — three timeframes (4H + 1H + 15m) ingesting and evaluable simultaneously, the three Phase A strategies bit-identical throughout. Phase B sign-off remains 2026-05-23 (`docs/operations/phase-b-complete.md`).

**Next:**
- **Strategy hunting** — Hunt 5 (Mean-rev + Tier-3 throttle) is now fully expressible via v1.2.B's `bars_since_last_at_least`; Hunt 6B (Intraday seasonality) via v1.2.C's `TimeOfDayCondition`. Re-extracting both is opportunistic strategy-hunting, not a sub-phase. The seed-and-trade leg waits for a strategy with a real edge — both B.7 (1H EMA crossover) and B.9 (15m BB+EMA200 regime) were correctly rejected by the gauntlet in Phase B; same fate awaits any strategy without genuine edge.
- **Phase C (multi-asset)** — deferred.
- **Phase D (live execution)** — deferred (and gated on far more than a date).
- **v1.1 indicator whitelist expansion** — Supertrend done (2026-05-22); ADX / Keltner / PSAR done (2026-05-23). All four shipped — see `docs/operations/v1.1-todos.md` for remaining follow-ups (extraction-prompt teaching audits, "spot" exchange string, max_drawdown >80% refusal flag, container deployment debt operator default).
- **Phase 5.2b (Railway deployment)** — frontend/deployment milestone, still open.

**Operational state of the paper-trading bot (2026-05-25, post-v1.2-rebuild):**
- 3 strategies actively evaluating: BB Breakout EMA200 4H BTC (v1 `breakout`), Golden Cross 50/200 SMA 4H BTC (v1 `ma_trend`), **Modern Turtle Donchian Breakout 4H BTC** (v2-native `template='spec'`, in warmup at 228/255 bars after the post-merge rebuild — warmup count preserved across the api+worker+trader_worker rebuild on 2026-05-25 09:43Z, first cycle resumed 36 s after restart; first live evaluation ~2026-05-30).
- `TRADER_TIMEFRAMES = "4h,1h,15m"` — three-TF ingestion live for BTC/USDT + ETH/USDT.
- Daily summary writes JSON + text to `data/daily-summaries/` at 00:05 UTC.

Phases 2–4, 5.1, 5.2a, Phase A, Phase B, and v1.2 schema additions are complete.

**Deployment-time env vars (must be set in Railway before Phase 5.2b launch):**
- `NEXT_PUBLIC_PLAUSIBLE_DOMAIN` — Plausible site key; unset = analytics disabled.
- `ADMIN_USERNAME` / `ADMIN_PASSWORD` — gate /admin/stats and the API's /admin/* routes. Both web and API need them.
- `DAILY_COST_CAP_GBP` / `GBP_USD_RATE` — daily Anthropic spend ceiling; defaults are conservative.
- Full catalogue lives in /docs/deployment/env-vars.md.

> **Update 2026-06-04 (bot state after the 30 May → 4 Jun trader-host sleep):** cycles resumed ~10:12Z on 2026-06-04. BTC/USDT 4H (288 candles) and 1H (481 candles) backfilled fully contiguous — zero gap (the exchange served the sleep-period candles; append-only `ON CONFLICT DO NOTHING` stitched them in). **Modern Turtle is now past the 255-bar warmup and live-evaluating** (it crossed the threshold ~2026-05-29, before the sleep; state rows resumed cleanly with no corruption / idempotency trip). No entry signal has ever fired (`trader_signals` empty). Seven strategy versions are approved+enabled in paper (6×4H + 1×1H, all BTC/USDT) — the four most recent are hunt-seeded (Hunts 19–21) and warming up. **Caveat:** the 15m series has an unrepaired ~2.85-day hole (05-30 11:30 → 06-02 08:15) from the wake backfill's 200-bar fetch cap; harmless to current strategies (none trade 15m), won't self-heal — see CLAUDE.md rule.

---

## Sacred design artifacts — full index

(CLAUDE.md keeps the load-bearing subset; this is the complete list.)

- /docs/strategy-spec.md — strategy spec v1.0; canonical source of truth for the strategy schema. Pydantic models, validation rules, backtest executor, and UI all derive from this document.
- /shared/src/marketmind_shared/schemas/strategy_spec/ — executable form of the spec doc above. Modular package: common/expressions/indicators/conditions/entry/exit/sizing/filters/costs/metadata/spec/validator/errors. Mirror of the bounds table lives in indicators.py — any change here requires a matching change in the spec doc.
- /shared/src/marketmind_shared/schemas/content/ — Phase 2.1 content schemas: IngestedContent discriminated union (YouTubeContent / ArticleContent / RawTextContent), Transcript, ExtractionInput. Same UTC-only datetime convention as Metadata.extracted_at.
- /tests/fixtures/strategies/ — golden test strategies (8 valid + 4 invalid with expected_error sidecars). All round-trip and bound checks parametrize over this directory.
- /infra/db/migrations/ — file-based SQL migrations applied by the worker at startup; the canonical source for the Phase 2.1 application tables (`ingested_content`, `transcripts`, `extracted_strategies`) plus the Phase 2.2 `extraction_costs` table.
- /docs/extraction-prompt.md — Phase 2.2 extraction prompt design doc. The canonical text lives in /workers/src/marketmind_workers/services/extraction_prompt.py (EXTRACTION_SYSTEM_PROMPT); any byte-level change to that constant invalidates the Anthropic prompt cache, so the design doc and the constant must stay in lockstep.
- /shared/src/marketmind_shared/schemas/extraction_report/ — Phase 2.2 ExtractionReport, ExtractedRule, AuthorClaim, ExtractionResult, ExtractionVerdict models.
- /docs/design/v2-phase-a-stateful-conditions.md — Phase A v2.0 stateful conditions design doc. The T1/T2/T3 capability, the regime_state / ratchet / prior_signal / prior_trade primitives, the iterative T3 engine + drift-parity gate, the SpecTemplate, A.4's state-aware overfitting composite weights, the §3.6/§3.7 extraction-prompt teaching fixes (regime_state hysteresis 2026-05-21; highest/lowest source convention 2026-05-22).
- /docs/design/v1.1-indicator-supertrend.md — v1.1 indicator whitelist expansion: Supertrend. Q1/Q2/Q3 design decisions, hand-roll rationale (no library in the stack ships one), Phase 3 validation result, known limitation (`_detect_axes` does not auto-sweep Supertrend params).
- /workers/src/marketmind_workers/backtest/iterative.py + iterative_live.py — the iterative Tier-3 simulator (batch backtest) and the sibling live per-bar stepper (trader). Drift-parity zero divergence is the load-bearing CI gate that lets the two paths coexist safely.
- /workers/src/marketmind_workers/trader/templates/spec_template.py — generic SpecTemplate. Runs any SpecTemplate-compatible StrategySpec in the trader, stateful or not. The hand-coded v1 templates (`bb_mean_reversion`, `breakout`, `ma_trend`, `rsi_mean_reversion`, `vcb`) are kept for the v1 strategies seeded before Phase A; new strategies route here.
- /workers/src/marketmind_workers/observability/ — daily summary observability: models, queries, render, the 00:05 trader_worker tick. Output to `data/daily-summaries/`.
- /docs/operations/phase-a-complete.md — A.7 sign-off record (2026-05-21) + trader_worker rebuild documentation.
- /docs/operations/phase-b-complete.md — Phase B sign-off record (2026-05-23, 10 sub-phases in one day). The Phase B hard-won knowledge lives here.
- /docs/design/v2-phase-b-lower-timeframes.md — Phase B lower-timeframes design doc. The B.1-B.10 sub-phases, per-sub-phase "Shipped" notes, Q1-Q6 resolutions (static fee + slippage tables, per-TF version split, sqrt(N) Brownian drift scaling).
- /docs/operations/fees.md and /docs/operations/slippage.md — B.1 + B.2 operator guides; quarterly manual refresh procedures for the per-exchange / per-symbol / per-tier fee + slippage tables.
- /workers/src/marketmind_workers/backtest/fee_model.py + slippage_model.py — B.1 + B.2 abstractions. Engine + iterative + benchmark all route through these instead of reading spec.costs directly; spec.costs is decorative for the engine after B.2 (kept on the schema for serialisation / UI display only).
- /workers/src/marketmind_workers/trader/drift.py:sqrt_n_scaling_factor + scaled_thresholds — B.6 per-TF drift threshold scaling. 4H is identity (bit-identity gate for existing Phase A strategies); 1H widens 2×, 15m 4×, 1d ≈0.41×.
- /docs/operations/daily-summary.md — operator guide for the daily summary report.
- /docs/operations/v1.1-todos.md — v1.1 follow-up findings (indicator whitelist expansion, candle backfill depth, `/data` uid, JS-rendered article ingestion, Phase A capability boundary, extraction prompt teaching audits).
- Migrations `0012_trader_v2_spec_template.sql` and `0013_v2_trader_strategy_state.sql` — the v2 schema additions (the SpecTemplate routing column and the `trader_strategy_state` table for live stateful execution).

---

## Phase 0 decisions (see /docs/adr/)
- **ADR-0001 — Monorepo with a uv workspace.** Single uv workspace at the repo root covers `api`, `workers`, `shared`; web is a standalone pnpm package. One `uv.lock` resolves all Python services together.
- **ADR-0002 — RQ over Celery for background jobs.** Chose RQ for solo-dev simplicity; Celery's brokers/backends/beat/routing surface area isn't worth the operational tax at this scale.
- **ADR-0003 — Ruff (lint + format) and Pyright, not Black + mypy.** Ruff replaces Black entirely (same style, ~30× faster); Pyright strict mode replaces mypy (better Pydantic/FastAPI inference, much faster).
- **ADR-0004 — Pydantic → JSON Schema → TypeScript pipeline.** Pydantic is the single source of truth for cross-service types; JSON Schema is the transport; `json-schema-to-typescript` produces the TS bundle the frontend consumes. One-way only.

## Phase 0 hard-won knowledge

Gotchas we hit and fixed. Don't re-learn these.

- **uv workspace members aren't installed by default.** The root `pyproject.toml` must list each member in `[project.dependencies]` AND map them via `[tool.uv.sources]`. Declaring them only under `[tool.uv.workspace] members = [...]` makes uv aware of them but does NOT install them when you run `uv sync` at the root. Symptom: `ModuleNotFoundError` for `marketmind_api` / `marketmind_workers` / `marketmind_shared` despite a "successful" sync.

- **`rq.job.JobStatus` is a `str` subclass but `str(member)` returns `"JobStatus.QUEUED"`, not `"queued"`.** Always normalize via `.value` when mapping (see `_normalize_rq_status` in `api/src/marketmind_api/routes/jobs.py`). Symptom: every job reports `failed` even when the worker logs "Job OK".

- **Docker images use `uv sync --no-editable`.** Editable installs put a `.pth` file in the venv pointing at the builder stage's `/build/...` source paths, which don't exist in the runtime image — imports fail at startup. The trade-off: bind-mounting source into containers for live reload doesn't update Python imports. Recommended dev workflow is running api/worker on the host (`uv run uvicorn ...`, `uv run python -m marketmind_workers.worker`), with only Postgres + Redis in Docker.

- **Corepack on `node:20.x-alpine` ships stale npm registry signing keys.** `corepack enable` fails with `Cannot find matching keyid`. Always `RUN npm install -g corepack@latest && corepack enable` (see `infra/web.Dockerfile`). Will recur on any old Node base image.

- **Multiple `tests/` dirs collide as one `tests` package** if any of them have `__init__.py`, producing `ImportPathMismatchError` at collection time. Solution: keep test dirs free of `__init__.py` and set `--import-mode=importlib` in `[tool.pytest.ini_options]`.

- **Always run commands from the repo root.** uv workspace resolution is anchored there. `uv run ...`, `uv sync`, `uv run pytest`, `uv run pyright`, `docker compose ...` all expect cwd at `<repo-root>/`. The only exceptions are `pnpm` commands, which run from `web/`.

## Phase 1 hard-won knowledge

- **Pydantic v2 discriminated unions need `model_rebuild()` after the alias definition** when variants forward-reference the union. Pattern: define all variant classes → define `T = Annotated[Union[...], Field(discriminator=...)]` → call `.model_rebuild()` on every variant that contains `"T"` (forward ref). Without the rebuild, Pydantic raises `PydanticUndefinedAnnotation` at first validate, not at import time — so test failures show up far from the cause.

- **`PydanticCustomError("slug", "message")` is the only way to set a stable `error_code`.** Pydantic's built-in `ValueError` produces `type="value_error"` regardless of the message. Tests that assert on `err.error_code` must therefore use custom errors in validators. We use this for every cross-cutting rule the test fixtures match against.

- **`str(StrEnum.member)` returns the value, not the qualified name** — unlike regular `Enum`. So `str(Timeframe.H1) == "1h"`. This is what we want for serialization but it differs from RQ's older `Enum` subclass behaviour (Phase 0 hard-won), so don't assume one model based on the other.

- **Pydantic int fields reject non-whole floats** even in lax mode. The test_bounds matrix nearly bit us: `(min + max) / 2` for an int field with bounds `(3, 200)` is `101.5` and gets rejected. When constructing test payloads for int params, detect "is this an int param" via `bound.min == int(bound.min) and bound.max == int(bound.max)` and cast.

- **`Path.with_suffix(".expected_error.json")` returns `foo.expected_error.json` only if the source path is `foo.json`** — i.e. it replaces just the last suffix component. For our sidecar lookup pattern that's exactly what we want; just be aware that `Path("foo.bar.json").with_suffix(".expected_error.json")` gives `foo.bar.expected_error.json`, replacing `.json` only.

- **The discriminator key shows up in `loc` tuples** in Pydantic v2 error reports. A failure inside `entry.condition` where the condition is a `compare` gets `loc=('entry', 'condition', 'compare', 'left', ...)`. We pass these through unchanged in `field_path`; tests use substring matching to avoid coupling to that detail.

- **Generated `web/src/types/generated/schemas.json` is gitignored.** The bundle is regenerated by `uv run python shared/scripts/export_json_schema.py`. CI doesn't run this currently — when Phase 2 starts consuming the TS types in the frontend, add a CI step that regenerates and diff-checks against committed types, OR commit the JSON. Decided to be made then.

## Phase 2.1 hard-won knowledge

- **Pydantic discriminated unions aren't BaseModels, so `model_json_schema()` doesn't exist on them.** Export them through `TypeAdapter(MyUnion).json_schema(mode="serialization")`. The JSON-Schema export script keeps two registries — a `dict[str, BaseModel]` for normal models plus a `dict[str, TypeAdapter[...]]` for unions — so each path uses the right call.

- **PEP 695 `type X = ...` aliases break Pydantic v2's discriminator inference** in some edge cases. We stuck with the older `X: TypeAlias = Annotated[U, Field(discriminator=...)]` form for `IngestedContent` (with `# noqa: UP040`). Ruff's UP040 wants to rewrite the alias; suppressing it locally is the right call here.

- **yt-dlp returns `_InfoDict` from `extract_info`, not `dict[str, Any]`.** Wrapping it in our own `_YoutubeDownloader` Protocol (with `cast("_YoutubeDownloader", YoutubeDL(...))`) gives us a small, stable surface to mock without dragging the yt-dlp type stubs through every test. Same pattern works for faster-whisper's `WhisperModel`.

- **`subprocess.run([...])` with `"ffprobe"` (not `/usr/bin/ffprobe`) trips ruff's S607.** Worker startup already guarantees ffmpeg is on PATH (`_check_ffmpeg` in worker.py), so a partial path is the intentional choice; `# noqa: S607` lives on the list literal, not on the call line — ruff attaches the violation to the argument, not the function.

- **`docker-entrypoint-initdb.d/` only runs files at the directory's top level, not subdirectories.** To apply our `infra/db/migrations/*.sql` on a fresh `compose up`, we mount the migrations dir alongside a small shell wrapper (`infra/postgres/apply-migrations.sh`) into the init dir. The worker re-applies the same files on startup (idempotent via `_schema_migrations`) so non-compose deployments still get the schema.

- **`testcontainers.postgres.PostgresContainer` produces a `postgresql+psycopg2://...` URL by default.** psycopg3 accepts the bare `postgresql://...` form, so we strip `+psycopg2` in the fixture. The bare testcontainer also doesn't ship `pgcrypto` (the migration uses `gen_random_uuid()`), so the test fixture installs the extension manually — production runs use `infra/postgres/init.sql` which has it.

- **`# noqa: <CODE>` must sit on the precise line that ruff blames, not the call line.** When `subprocess.run(...)` triggers S607, S607 attaches to the list-literal line (`[...`), not to `subprocess.run`. Putting the noqa on the wrong line leaves an `RUF100 unused noqa` plus the original violation. Read the ruff caret carefully.

- **The Anthropic SDK can be installed but not imported.** Phase 2.1 deliberately keeps the SDK out of every production code path so the LLM prompt design can land cleanly in 2.2. `workers/services/llm.py` reads `ANTHROPIC_API_KEY` from env but never instantiates a client. A test asserts the module's source contains neither `import anthropic` nor `from anthropic` to keep the rule load-bearing.

- **RQ's `job.meta` is the cheapest way to round-trip the `JobKind`.** GET `/jobs/{id}` was previously hardcoded to `JobKind.DUMMY` because Phase 0 only had one kind. Storing `{"marketmind:kind": kind.value}` in meta at enqueue time and parsing it back on fetch is enough — no sidecar Redis hash, no DB row, and the fallback path (legacy Phase 0 jobs without the meta key) still parses as DUMMY.

## Phase 2.2 hard-won knowledge

- **The Anthropic tool input_schema embeds StrategySpec's `$defs` at the input_schema root, not nested under `properties.spec`.** Pydantic emits a self-contained schema with refs like `#/$defs/SomeType`. If you leave those defs inside the spec subtree, the model will get ref-resolution errors because they look up against the input_schema root. Pop the spec's `$defs`, fold them into the tool's top-level `$defs`, then `oneOf: [spec_body, {type: "null"}]`. The Anthropic side then resolves cleanly.

- **`lru_cache(maxsize=1)` on the tool builder is load-bearing for cache stability.** The prompt-cache key is the byte representation of the system prompt + tool definition. If the tool definition is rebuilt fresh per call, Python `dict` insertion order and Pydantic field ordering will eventually drift and invalidate the cache. Returning the same dict reference every call keeps the cache warm.

- **Anthropic SDK's usage shape uses `cache_creation_input_tokens` (writes) and `cache_read_input_tokens` (reads), and `input_tokens` excludes both.** A 22k-token cached system prompt + tool shows up as roughly `input_tokens=transcript_size, cache_read_input_tokens=22000`, not as `input_tokens=22000 + transcript`. Cost math has to handle the three buckets separately: writes are 1.25x normal input, reads are 0.1x. Getting this wrong leads to undercounting bills by ~10x in steady state.

- **`pyproject.toml`'s `filterwarnings = ["error", ...]` blocks faster-whisper's integration test through a transitive deprecation in `huggingface_hub`.** Same gotcha pattern is now visible: `huggingface_hub` emits `DeprecationWarning: hf_xet.download_files() is deprecated` during model downloads, and pytest's strict filter promotes it. The integration test is opt-in (`-m integration`) and can be re-invoked with `-W "ignore::DeprecationWarning"`. Worth extending the filterwarnings allowlist next time the file is touched.

- **The model wraps tool inputs in markdown fences ~10% of the time when the system prompt forbids them.** This was visible in the no-tool-use experiments and was fixed by switching to `tool_choice: {"type": "tool", ...}` — tool-use payloads come back as parsed dicts, so the fences problem disappears entirely. If we ever fall back to JSON-mode output, strip fences defensively.

- **`exactOptionalPropertyTypes: true` in tsconfig forbids passing `undefined` to fields typed as optional.** Caused friction when building `RequestInit` in `lib/extraction.ts` — `signal: signal` fails if signal is undefined. The trick: `...(signal ? { signal } : {})`. Same pattern in the old `lib/api.ts`; the new `extraction.ts` uses it.

- **`@testing-library/react`'s auto-cleanup hook doesn't fire when tests use dynamic `await import()` patterns.** Symptom: DOM from one test bleeds into the next, surfacing as "element already in document" failures. Fix: import `cleanup` and wire it into `afterEach` in `vitest.setup.ts`. Without this, tests pass individually but fail when run together.

- **Anthropic SDK doesn't reject unknown `tool` fields, so a `cache_control` typo (e.g., `cache_controll`) fails silently — no caching, no error.** Sanity-check that `cache_read_input_tokens` is non-zero on the second extraction in a session before declaring the cache live.

## Phase 2 deployment debt — six bug categories caught by smoke testing

The full Phase 2 stack (api + worker + web + Postgres + Redis in compose) didn't actually work end-to-end the first time, despite a clean CI suite and clean pyright. Six categories of bug surfaced through five smoke-test attempts, each requiring a fix before the test could continue. The categories themselves repeat across phases — worth recognising the shape early.

- **Container dependencies.** Symptom: worker exited immediately on boot with `ffmpeg_missing`. The Phase 2.1 startup check was correct; the Dockerfile just didn't install ffmpeg. Fix shape: `apt-get install -y --no-install-recommends ffmpeg` in the runtime stage. CI never builds the worker image, which is how this stayed hidden.

- **Service-to-service env wiring.** Symptom: every DB-touching worker job failed with `connection to server at "127.0.0.1" failed`. Phase 2.1 added `database_url` to `WorkerSettings`, but `docker-compose.yml`'s worker service block never passed `DATABASE_URL` through (the api block did). Fix shape: one new env var on the compose service + `postgres` added to `depends_on`. Same shape recurred in Phase 2.2 for the Next.js server-component fetch (`API_URL_INTERNAL`).

- **Package resource loading: the `parents[N]` anti-pattern.** Symptom: `migrations_directory_empty` warning at worker boot; later, `schemas.json not found at /opt/venv/lib/web/src/...` at first LLM call. Both modules resolved external resources via `Path(__file__).resolve().parents[N]`, which works on the host (editable install) but lands at `/opt/venv/lib/` in a `--no-editable` wheel install. Fix shape: hatch `[tool.hatch.build.targets.wheel.force-include]` to copy the resource into the wheel + `importlib.resources.files()` lookup with a parents[N] fallback for editable installs. Audit `grep -rn "parents\[" workers/src/ shared/src/ api/src/` periodically.

- **Schema/code agreement on nullable columns.** Symptom: `psycopg.errors.NotNullViolation: null value in column "spec_json"` the first time a refusal verdict reached persistence. Phase 2.1 wrote the column as `NOT NULL` when extraction was a stub; Phase 2.2's ExtractionResult model explicitly allows `spec=None` for refusal verdicts. Fix shape: `ALTER COLUMN ... DROP NOT NULL` migration + a `COMMENT ON COLUMN` documenting why it's nullable. The integration test that would have caught this exists (`tests/test_db_integration.py::test_save_and_fetch_extraction`) but CI doesn't run integration tests.

- **Atomic persistence of related rows.** Symptom: a real Anthropic call succeeded (~$0.20 paid), but the persistence step downstream raised — and the cost row never got written because `save_extraction_cost()` was a separate transaction running after `save_extraction()`. Fix shape: combine the two writes inside a single `conn.transaction()` block (`save_extraction_with_cost`) so they commit or fail together. General rule: a row that records "we already paid for this" should commit before or with the row that depends on it, never after.

- **Next.js server/client URL split.** Symptom: `/strategies/[id]` returned 500 with `ECONNREFUSED 127.0.0.1:8000` in the web container's logs while the same URL worked fine from the browser. `NEXT_PUBLIC_API_URL` is bundled into the client JS for the browser; server components fetch from inside the web container, where the same URL points at the wrong host. Fix shape: two env vars (`NEXT_PUBLIC_API_URL` for the browser, `API_URL_INTERNAL` for SSR) + a small `apiBaseUrl()` helper that picks the right one via `typeof window === "undefined"`. The Phase 2.2 final report flagged this as "worth confirming end-to-end" — and it bit on the very next smoke test.

The thread running through all six: **a passing CI suite + a clean pyright means the unit-level contracts hold. It does NOT mean the full stack composes correctly.** Every one of these would have shown up in a single end-to-end CI step that brings up the compose stack and drives one extraction. Filed as Phase 3 housekeeping.

---

## Phase A — stateful condition schema (A.7 signed off 2026-05-21; merged to main and deployed 2026-05-22)

Phase A added schema-v2.0 stateful conditions to MarketMind so the system can describe and trustfully backtest path-dependent strategies. The capability is three-tiered:

- **Tier 1 (T1, bounded-window)** — composes existing v1 condition shapes; no new state.
- **Tier 2 (T2, latched / regime)** — `regime_state` (a latched boolean with distinct enter/exit triggers — hysteresis) and `ratchet` with `reset="never"` (a running max/min over the full series).
- **Tier 3 (T3, trade- or signal-outcome dependent)** — `prior_trade` / `prior_signal` predicates, `ratchet reset="per_trade"` (a trailing extremum restarted at each entry).

**Architecture in one line:** extraction (LLM with the v2-aware prompt) → schema validation (strict, extra-forbid Pydantic) → router (vectorbt for non-T3, iterative for T3) → A.4 state-aware overfitting composite → SpecTemplate-or-v1-template seed → live trader running the SpecTemplate (state persistence + idempotency guard).

**Two backtest engines, one safety gate:**
- `backtest/translator.py` + vectorbt for T1/T2 — vectorised, the prior pipeline kept untouched.
- `backtest/iterative.py` for T3 — a per-bar Python simulator tracking `Tier3State` (signal history, completed trades, ratchet extrema with `reset="per_trade"`) across the run. The router picks the engine from `spec_uses_tier3(spec)`.
- **Drift parity is the load-bearing test.** For any non-T3 spec, the iterative engine and the vectorbt engine must produce bit-identical equity curves and trade ledgers — zero divergence. This is what made the "sibling live evaluator" pattern (B3 in A.6) safe: `iterative_live.py` reuses `iterative.py` primitives for the per-bar incremental step the trader needs, and drift parity proves the two paths compute identically.

**State persistence in the trader:** migration 0013 (`trader_strategy_state`) — one row per (version, symbol, timeframe), JSON-serialised `StrategyState` (T2 latched booleans, T3 `Tier3State`), with two safety mechanisms — an idempotency guard (`pair_state_guarded` if the same `candle_close_ts` is seen twice in a single version's history) and a corrupt-state auto-disable (`pair_state_disabled` plus a `trader_alerts` row when the state JSON fails to deserialise). The signal engine fetches `_TIER3_FETCH_BARS=200_000` candles for T3 specs to give the full-history iterative engine enough lookback.

**A.4 state-aware overfitting weights.** The composite re-weights when `spec_uses_stateful_v2(spec)` is True — **MC 0.10 (down from 0.25), WF 0.50 (up from 0.35)** — because the Monte Carlo permutation test compares against drift-preserving reshuffles and can understate a defensive stateful strategy; walk-forward stays the gold-standard signal. Tier-1 specs use the original weights. The gate is `spec_uses_stateful_v2(spec)` (the actual-use check), **not** `schema_version == "2.0"` (which the LLM can over-declare conservatively).

**Indicator whitelist — 19 indicators** after the v1.1 batch additions: SMA, EMA, WMA, RSI, MACD, Stochastic, ATR, Bollinger, StdDev, VolumeSMA, OBV, VWAP, Highest, Lowest, Returns, **Supertrend** (2026-05-22; hand-rolled — no library has it; multi-output value + direction), **ADX** (2026-05-23; ta-backed; single-output scalar trend strength), **Keltner** (2026-05-23; ta-backed; multi-output upper/middle/lower, modern EMA-based variant), **PSAR** (2026-05-23; ta-backed; multi-output value + direction). Known limitation: `_detect_axes` (overfitting parameter sweep) detects `period` on sma/ema/rsi/wma/adx/keltner; atr / bollinger / macd / highest / lowest / supertrend.atr_period / keltner.atr_period / supertrend.multiplier / psar.step+max_step / bollinger.std_dev are un-swept. A generic `_detect_axes` widening across all `INDICATOR_RULES[*].numeric` params is logged as a v1.1 follow-up (would change overfitting outputs for existing strategies — a deliberate, not-during-feature-add change).

**Operational state (as of 2026-05-23):** 3 strategies seeded and approved into the paper bot — Bollinger Band Breakout EMA200 4H BTC (v1 `breakout` template), Golden Cross 50/200 SMA 4H BTC (v1 `ma_trend`), and **Modern Turtle Donchian Breakout 4H BTC** (the first v2-native `template='spec'`). Modern Turtle is in warmup (214/255 bars); first live evaluation around 2026-05-30 — the first time A.5b state persistence runs in production cycle conditions. Daily summary observability writes JSON + text to `data/daily-summaries/` at 00:05 UTC; first production fire confirmed 2026-05-23 00:05Z.

### Design-then-implement, the pattern that worked

Every Phase A sub-phase followed this loop: write a design doc (with explicit Q&A on the contentious decisions), get sign-off (sometimes self-sign-off after discovery surfaced no real fork), then implement in narrow commits each with their own tests. A.5a (SpecTemplate), A.6 (iterative_live + drift parity), A.7 (sign-off + rebuild), and the v1.1 Supertrend addition — all used the same pattern. The discipline earns its keep when discovery genuinely surfaces a fork (B1 vs B3 in A.6 — needed user input) or a blocker (Phase A's first attempts surfaced the regime_state degenerate-example problem and the highest/lowest `source` convention before any code was wasted).

### Extraction prompt teaching audits — the meta-pattern

Two consecutive sessions surfaced the same bug shape: the extraction prompt's worked example for a load-bearing primitive was either **degenerate** (regime_state's only example was the same-threshold case → the model treated regime_state as a verbose `compare`) or **missing entirely** (no `highest`/`lowest` example → the model omitted the required `source` param convention, schema validation rejected the spec, `_downgrade_to_refusal` masked it as `not_extractable`). The fix shape is the same each time: add a load-bearing worked example. The LLM is generally capable of using v2 primitives correctly once shown a non-trivial case — but a degenerate or missing example silently teaches the wrong thing. The discipline is now codified: when fixing one primitive's teaching, audit related primitives in the same pass. Logged at `docs/operations/v1.1-todos.md` ("Extraction prompt teaching audits"); design-doc record at `docs/design/v2-phase-a-stateful-conditions.md` §3.6 + §3.7.

### Phase A capability boundary — what stateful does and does not buy you

Stateful primitives are for **hysteresis-banded strategy-level state** — regimes with distinct enter/exit triggers, trade-outcome dependencies, ratcheting trailing logic. They are **not** for "the indicator itself is recursive." A pure Supertrend strategy is Tier-1 at the spec level — `crossover(supertrend.direction)` is a stateless compare; the recursion lives inside the indicator function (just like EMA's). Wrapping a self-latching indicator in `regime_state` is the degenerate pattern the prompt warns against. The boundary surfaced concretely while seeding Supertrend; recorded at `docs/design/v1.1-indicator-supertrend.md` (Phase 3 section).

## Phase A hard-won knowledge

Gotchas, lessons, and meta-patterns from Phase A — the stateful condition schema and its deployment into the paper-trading bot.

- **Always check tooling first when adding new capabilities.** The Supertrend v1.1 addition's Q2 discovery audited the env (`vectorbt` 0.28.5, `ta`, `pandas_ta`, `talib`) BEFORE deciding to hand-roll. Some hits are obvious (the post-Supertrend recommendation confirmed `ta` ships ADX, Keltner, and PSAR — those won't need hand-rolling); some genuinely absent (vectorbt has `OHLCSTX` but no Supertrend — those are stop-exit signal tools, not the indicator). A five-minute audit prevents a wrong-direction implementation.

- **Bias toward proactive auditing when fixing one primitive's teaching.** The regime_state hysteresis fix (2026-05-21) and the highest/lowest source-convention fix (2026-05-22) were the same bug shape twice. The second fix triggered a full audit pass of every primitive's prompt example against the test "if the LLM only ever saw this example, would it know how to use the primitive in a non-trivial case?" That sweep found `scaled` thinly-taught and patched it in the same commit. Pattern: a teaching gap discovered in the wild → fix it AND sweep related primitives.

- **A non-stateful strategy with no `stop_loss` and no matching v1 hand-coded template hits a real seedability wall.** The SpecTemplate requires every entry to carry a protective `stop_loss`-type exit — genuine flash-crash protection, not an arbitrary rule (a `condition` exit on a trend flip only acts at bar close, no help during a fast crash). A pure Supertrend strategy (exit = trend flip only) is faithfully extracted, passes the gauntlet, but is un-seedable until paired with a hard stop. Adding a 2×ATR ratcheting stop on top of Supertrend's own 3×ATR band makes them fight each other (the stop noise-triggers); OOS collapses, gauntlet says `likely_overfit`. The lesson: when seeding requires a primitive (`stop_loss`) the strategy genuinely lacks, the failure mode is structural — not a tooling bug.

- **The gauntlet's job is to refuse bad strategies. Gauntlet rejection is the system working.** Across Phase A's seeding sessions: RSI-pullback → `likely_overfit`, Supertrend-with-2×ATR-stop → `likely_overfit`, Modern Turtle → `likely_robust` (the one that seeded). Each rejection saved a paper-trading slot from a bad strategy. MarketMind's north star — "tells users whether trading strategies actually work" — depends on this discipline being literal, not soft.

- **Drift parity tests are the discipline that lets "sibling evaluator" patterns be safe.** A.6 chose B3 (sibling live stepper `iterative_live.py`, reusing primitives from `iterative.py`) over B1 (refactor `iterative.py` to support live stepping) because the drift-parity test catches any divergence with zero ambiguity. Without that test, B3 would be a permanent maintenance burden ("are the two paths still equivalent?"); with it, the live stepper is provably an incremental view of the batch simulator.

- **Container rebuild deployment: `--no-deps` to scope tightly.** `docker compose up -d --build <service>` will rebuild and recreate dependency containers with `build:` sections (the `web` rebuild this phase inadvertently recreated the `api`). Always pass `--no-deps` for a one-service deploy. The `trader_worker` is the highest-risk container to rebuild because it holds in-memory state (the RQ scheduler lock, the in-process tick scheduling, the strategy-loader cache); restart it deliberately, not as a side-effect. `api` and `worker` can be rebuilt safely (their state lives in Postgres / Redis).

- **`spec_uses_stateful_v2(spec)`, not `schema_version == "2.0"`, is the right gate for state-aware behaviour.** A spec can be `schema_version 2.0` and use zero stateful conditions (the LLM declared 2.0 conservatively but actually wrote a Tier-1 strategy — observed on the Supertrend extraction). All state-aware code (overfitting weights, candle backfill depth, SpecTemplate runtime branches) checks `spec_uses_stateful_v2(spec)` — the actual-use check, not the declared-version check.

- **Bar-index stability assumption for T3.** The T3 `SignalHistory` uses absolute bar indices into `trader_candles` for the same symbol+timeframe. This requires `trader_candles` to be append-only — no re-ingestion that re-numbers bars. The trader ingestion enforces this via `INSERT ... ON CONFLICT (symbol, timeframe, open_ts) DO NOTHING`, so a re-fetched bar with the same `open_ts` is a no-op. Any future migration that compacts or re-numbers candles must also remap T3 `SignalHistory` indices in `trader_strategy_state`.

- **Seed script routing: `--template` omitted ⇒ auto-route to `template='spec'` for any SpecTemplate-compatible spec.** This holds for stateful AND non-stateful specs since `c1c7a0a` (2026-05-23) — the prior gate of "stateful only" broke for non-stateful new-indicator strategies. `--template` supplied ⇒ map to a v1 hand-coded template (rejected for stateful specs). The `--template` choices remain the five v1 templates; `spec` is not a valid `--template` value (it's only reachable via the auto-route).

- **Modern Turtle is the first v2-native strategy in paper.** Its first evaluation (~2026-05-30, when its bar history reaches 255) is the first time A.5b state persistence runs in production cycle conditions. Pre-deployment was test-fixture + drift-parity validated; this is the first live test of the trader's state-write path under real cycle cadence. Worth watching closely on day one — does a `trader_strategy_state` row appear, do subsequent cycles read it back cleanly, does the idempotency guard ever trigger.

---

## Phase B — lower timeframes (B.10 signed off 2026-05-23; shipped directly to main in one day)

Phase B added 1H and 15m as first-class timeframes alongside the v1 4H. Ten sub-phases (B.1-B.10) all shipped to `main` on 2026-05-22 → 2026-05-23. The full sign-off + sub-phase summary + commit hashes live in `docs/operations/phase-b-complete.md`; the design doc with per-sub-phase "Shipped" notes is `docs/design/v2-phase-b-lower-timeframes.md`.

**Architecture in one line:** the Phase 1 design hypothesis — "the architecture is already TF-agnostic" — held literally. Phase B was cost-model honesty + observability tuning, not architectural rework. `ingestion.py` and `signal_engine.py` already iterated the env-var-driven `TRADER_SYMBOLS × TRADER_TIMEFRAMES` product; the `trader_candles.timeframe TEXT NOT NULL` column accepted any TF string without migration.

**Shipped capability:**
- `FeeModel` (B.1) + `SlippageModel` (B.2) — Protocol-backed abstractions, per-exchange / per-symbol / per-side / per-volume-tier tables, replacing the `spec.costs.{commission,slippage}_pct` direct read. Spec.costs is now decorative for the engine (kept for serialisation / UI display). Operator-facing quarterly refresh procedures in `docs/operations/fees.md` and `slippage.md`.
- `TRADER_TIMEFRAMES = "4h,1h,15m"` (B.3 + B.8) — three timeframes ingesting and evaluable simultaneously. trader_worker rebuilt twice (once per TF added). The 3 Phase A strategies stay 4H-only via the `version.timeframes ∩ config_timeframes` intersection gate; bit-identical throughout.
- Pre-fetched parquet fixtures (B.4 1H, B.8 15m) for CI-reproducible perf-regression tests. 1H: 55,912 bars, 2.6 MB, < 5 s budget (median 1.05 s). 15m: 223,527 bars, 9.6 MB, < 8 s budget (median 4.44 s). Linear scaling vs 4H (4× and 17× respectively) validates the iterative engine's O(bars × indicators) bound.
- B.6 drift threshold sqrt(N) Brownian scaling — `sqrt_n_scaling_factor("4h")=1.0` (identity, bit-identity gate), `1h=2.0`, `15m=4.0`, `1d≈0.41`. `_classify_health` gained three optional threshold kwargs defaulting to pre-B.6 constants; existing 28 drift unit tests pass unmodified.
- B.7 + B.9 — two real-strategy pipeline tests at 1H and 15m. Both extracted cleanly through the gauntlet; both correctly rejected (`mixed_signals` 56/100 and `likely_overfit` 61/100 respectively). The end-to-end pipeline IS proven at both lower TFs; the seed-and-trade leg waits for a strategy with a real edge.

**Phase B hard-won knowledge** (full version in `phase-b-complete.md`):

- **Container deployment debt is wider than the Phase A pattern suggested.** Phase A's "rebuild `trader_worker` deliberately" was correct for trader-specific changes (env vars, scheduled-job lifecycle) but does NOT extend to shared-package additions. Phase B surfaced this three times: B.7 needed an api rebuild for v1.1 PSAR fields; B.8 discovered worker was pre-B.1 (just produced correct numbers by coincidence); B.10 closed the gap with a worker hygiene rebuild. **Operator default going forward:** after any indicator-whitelist / schema / v2-primitive addition, rebuild ALL three Python containers (`api`, `worker`, `trader_worker`). ~6-8 min back-to-back with `--no-deps` on each.

- **Two textbook strategies rejected — same pattern as Phase A.** EMA crossover at 1H (B.7) failed for walk-forward degradation 0.0 + no Monte-Carlo edge. BB+EMA200 at 15m (B.9) failed for OOS positive_rate 0.0 + 99.6% max drawdown. Adds two more data points to the "gauntlet's job is to refuse bad strategies" discipline — RSI-pullback (Phase A), Supertrend-with-2×ATR-stop (Phase A), EMA crossover 1H (B.7), BB+EMA200 15m (B.9). **Pattern: design-then-implement + an honest gauntlet is what makes the seeding workflow safe to automate.**

- **Tier-2 routing first exercised in production via B.9.** The A.4 state-aware weights (WF=0.50, MC=0.10 vs Tier-1 base 0.35 / 0.25) routed correctly via `spec_uses_stateful_v2(spec) == True`. The composite contributions in B.9's overfitting result confirm the weights applied as designed. The actual-use check (not `schema_version == "2.0"`) remains the right gate.

- **Linear scaling held at 17× density.** Iterative engine perf measured on the same Modern Turtle spec at 4H / 1H / 15m: 0.256 s → 1.090 s → 4.440 s. Within 8% of strict linear. Validates the engine's per-bar O(indicators × bar) bound. Extrapolating: 5m (~13 s) plausibly within budget; 1m (~65 s) would warrant attention. The deferred "vectorised T3 engine" optimisation is NOT justified.

- **`max_drawdown > 80%` is qualitatively different from sharpe-of-noise overfitting.** B.9's strategy lost 99.6% of capital but landed at composite 61/100 (`likely_overfit`) — same band as B.7's "sharpe-of-noise" 56/100. The composite doesn't distinguish "actively destroys capital" from "no real edge." Logged as a v1.1 follow-up: soft two-tier flag (80-95% warning, >95% hard refusal).

- **`spot` exchange-string quirk** — B.7 and B.9 extractions produced `instrument.exchange = "spot"` instead of `"binance"`. Falls through FeeModel / SlippageModel fallback path; matches binance_spot defaults for BTC/USDT (10 / 5 bps) so backtest math unaffected. **Risk:** future non-default tables on another venue would silently use wrong fallback fees. Fix: extraction-prompt teaching audit + defensive `_exchange_key()` mapping. Logged in v1.1-todos.

- **`_MIN_WINDOW_BARS = 200` floor on SpecTemplate** — even a no-indicator spec needs 200 closed candles before first evaluation. At 4H = ~33 days; at 1H = ~8 days; at 15m = ~2 days. The synthetic cycle tests in B.5 + B.8 demonstrated this exactly (199 → 200 hit the floor on the next bar close). Any future strategy seeded at a lower TF needs to wait this many bars before first live evaluation.

- **Fee / slippage default asymmetry (10 / 5 bps).** Intentional — spreads on BTC/USDT majors are tighter than round-trip commission. Explicitly asserted in `test_default_model_returns_5_bps_for_btc_usdt_taker` so a future "they're both costs, must be equal" assumption can't drift them silently.

- **Indicator whitelist now at 19** (post-v1.1): SMA, EMA, WMA, RSI, MACD, Stochastic, ATR, Bollinger, StdDev, VolumeSMA, OBV, VWAP, Highest, Lowest, Returns, **Supertrend** (2026-05-22, hand-rolled), **ADX**, **Keltner**, **PSAR** (all 2026-05-23, ta-backed). The Phase B operator-default for container rebuilds applies whenever this list grows.

---

## v1.2 schema additions (signed off 2026-05-25; merged + tagged v1.2.0-final at `913994a`)

v1.2 added five new schema primitives — three Conditions, one Expression, one TakeProfitMethod — all surfaced by real-strategy extractions in the post-Phase-B 9-hunt era. Each primitive closes a documented `partially_extractable` or `not_extractable` outcome from that era. Five sub-phases (A-E) plus baseline + sign-off (7 total) shipped in 21 commits across 2026-05-24 → 2026-05-25. Full sign-off + retrospective in `docs/v1.2_retrospective.md`; per-sub-phase regression record in `docs/operations/v1.2-final-regression.md`; design pass in `docs/design/v1.2-schema-additions.md`; CHANGELOG.md (new top-level file).

**Architecture in one line:** v1.2 was purely additive at the schema level. The Expression / Condition / TakeProfitMethod discriminated unions each grew by one variant; dispatchers grew matching branches; existing strategies' evaluator paths are byte-identical. No engine rework, no migration, no architectural change.

**Shipped capability:**
- `PercentileExpr` (v1.2.A) — Expression variant. `percentile_rolling(inner, window)` returns the rank-as-fraction of the most recent value within a trailing window (bounds 10..10_000). Wraps any Expression. Surfaced by Hunt 3 (Momentum + ATR-percentile). `_detect_axes` recognises `PercentileExpr.window` via the new `SweepAxisKind.PERCENTILE_WINDOW` enum entry.
- `prior_trade(predicate="bars_since_last_at_least")` (v1.2.B) — 5th `PriorTradeCondition.predicate` literal, time-based re-entry throttle distinct from the four outcome-based predicates (win / loss / consecutive_wins / consecutive_losses). `n` upper bound widened 100 → 100_000 to accommodate bars-since use cases. `TradeHistory.evaluate_predicate` signature widened with keyword-only `current_bar: int | None = None` — backward-compatible (outcome-based predicates ignore the new arg). Surfaced by Hunt 5 (Mean-rev + Tier-3 throttle).
- `TimeOfDayCondition` (v1.2.C) — 13th Condition variant. UTC hour-of-day gate with wrap-around windows (start > end spans midnight) and inclusive/exclusive end. Surfaced by Hunt 6B (Intraday seasonality).
- `DayOfWeekCondition` (v1.2.D) — 14th Condition variant. UTC weekday gate (pandas convention Mon=0..Sun=6) with at least one weekday, no duplicates. Family-extension of v1.2.C — same shape pattern, copy-paste ergonomic.
- `TakeProfitAtrMultiple` (v1.2.E) — 4th TakeProfitMethod variant. `mult × ATR(atr_period)` above entry, symmetric to `StopLossAtrMultiple` (identical Pydantic bounds: atr_period int 2..100, mult float 0..20). LONG via iterative engine; SHORT via vbt with `direction="shortonly"` flipping sign internally. `_compute_take_profit` in `spec_template.py` gained `candles: pd.DataFrame | None = None` for the ATR branch.

**v1.2 hard-won knowledge:**

- **Drift parity has two distinct shapes — pick the right one per primitive.** Cadence parity (incremental-step `iterative_live.py` vs one-shot `iterative.py` — same engine, different stepping) is the load-bearing gate for stateful conditions (Tier-2 / Tier-3) where state-update semantics must be bit-identical across the live-stepper and the batch backtest. **Cross-engine envelope** (vbt vs iterative trade-count within ±2×) is the right gate for purely-additive primitives where known engine differences (exit-tie-break ordering, MACD line vs histogram lag, slight slippage rounding) mean strict trade-ledger bit-identity is impossible by construction. v1.2.A's PercentileExpr and v1.2.C/D's TimeOfDay/DayOfWeek conditions used the cross-engine envelope with one additional dispatcher-identity check (`pd.testing.assert_series_equal` on the helper output) for bit-identity at the dispatcher level — proving the engines compute the same Series even when the downstream trade ledger diverges. v1.2.B's `bars_since_last_at_least` used cadence parity because it touched the Tier-3 history state. v1.2.E's TakeProfitAtrMultiple used the cross-engine envelope on iterative LONG plus a vbt SHORT smoke test (iterative is long-only). Picking the wrong shape costs ~1 hour debugging a gate that can never go green.

- **Keyword-only-with-default signature widenings preserve byte-identity of existing call sites.** v1.2.B's `evaluate_predicate(..., *, current_bar: int | None = None)` was the highest-risk single touch in v1.2 — every existing call site continued to work without change because the new parameter is keyword-only and defaults to None (which the four outcome-based predicates ignore). Pyright caught every call site that COULD use the new parameter; the ones that don't need it pass through cleanly. This is the right pattern for any backward-compatible signature widening in v1.3+ — keyword-only, default to None, sentinel-checked at the receiver. Avoid positional widenings (force every call site to change) and avoid Optional defaults that change semantics for existing callers (e.g., a `mode="strict"` default that wasn't the old behavior).

- **Test docstrings written against theoretical mental model — empirics differ. CODIFIED standing rule for v1.3+.** Three citations across v1.2 alone: (1) v1.2.B drift-parity assertion `>=2 trades` was empirically `>=1` on the EMA(5/15) fixture (throttle gate effectively same as sticky gate at 1 trade); (2) v1.2.C TimeOfDayCondition end-to-end test asserted `entry_hour in 10..18` based on signal-at-t-fill-at-t+1 model, empirics showed `9..17` because the iterative engine reports `entry_time` as the SIGNAL bar's open time, not the fill bar's; (3) v1.2.E TakeProfitAtrMultiple drift parity expected TP fires at `entry + 2×ATR ≈ 104` with synthetic uptrend data, empirics fired immediately because the first bar was pre-warmup (atr[entry_bar] = NaN, graceful-degradation collapsed tp_level to entry, intrabar high triggered TP trivially). Fix shape: RUN the test once with print statements showing actual engine output (entry_time / entry_hour / entry_weekday / tp_price / fill_bar / fill_price), DOCUMENT the empirical finding in the test docstring, THEN encode the assertion against actual values. 30-60 s empirical step saves 5-15 min of post-hoc debugging. Mandatory for v1.3+ exit primitives, new dispatcher branches, and any test that asserts on `entry_*` / `exit_*` / `fill_*` numeric values.

- **Pyright is the completeness oracle for discriminated-union extensions.** Adding a variant to a Pydantic discriminated union (TakeProfitMethod, Condition, Expression) breaks pyright exhaustiveness on every downstream dispatcher. Pyright caught 3 dispatchers during v1.2.E that needed branches added (iterative `_tp_level`, vbt `_vbt_take_profit`, trader `_compute_take_profit`). **After extending any Pydantic discriminated union, run pyright BEFORE writing tests** to inventory the downstream code that needs branches. The brief's "commit 1 = schema only, commit 2 = engine" structure does NOT work for union extensions — pyright exhaustiveness force-folds schema + engine plumbing into one commit. Plan for "schema + engine plumbing in commit 1, then tests + prompt teaching in subsequent commits" as the realistic shape.

- **Container rebuild discipline applies to api + worker + trader_worker for any schema-affecting change.** Same as Phase B's load-bearing finding — extended to all schema additions, not just indicators. The rebuild itself is benign for the running paper bot when the seeded strategies don't touch new primitives: v1.2.0-final's post-merge rebuild on 2026-05-25 preserved Modern Turtle's warmup count (228/255 unchanged across restart), first cycle resumed 36 s after `up -d --force-recreate`, queue depths stayed at zero, zero alerts fired. The warmup count is reconstructed from `trader_candles` on each cycle (not from in-memory state), so restart-safety is structural. Use `--no-deps` to scope the rebuild tightly (avoid recreating `web` / `postgres` / `redis` as side-effects).

- **Hunt-era empirical demand is the most efficient way to find missing schema primitives.** 5 of the 9 post-Phase-B hunts (~55%) surfaced primitives that became v1.2 sub-phases. The hunts paid for themselves in real-strategy expressiveness gains. The remaining 4 hunts either rejected cleanly (gauntlet found no edge — same value as B.7 and B.9) or surfaced soft gaps (log_returns ≈ simple_returns at small per-bar magnitudes — deferred). The pattern for v1.3 primitive planning: run a hunt batch first, let real-strategy `partially_extractable` outcomes surface the gaps, then design the primitives that close those gaps. Do NOT speculate primitives in isolation — every speculative primitive is unbacked by extraction demand.

- **The empirical-inspection rule is co-equal in importance with Phase A's extraction-prompt-teaching audits and Phase B's container-rebuild discipline.** Three project-level standing rules from three phases:
  - Phase A: when fixing one extraction-prompt primitive's teaching, audit the related primitives in the same pass (degenerate or missing worked examples silently teach the wrong thing).
  - Phase B: after any indicator-whitelist / schema / v2-primitive addition, rebuild api + worker + trader_worker (~4-8 min back-to-back with `--no-deps`).
  - v1.2: before encoding any end-to-end numeric assertion in a test, RUN with prints → DOCUMENT in docstring → THEN encode against actual values.

- **v1.2 metrics.** 21 commits across 7 sub-phases. Tests 1099 → **1199** (+100; design estimated +30, exceeded by 70). Drift parity 5 → **76** gates (+71 cases), zero divergence. Suite wall-clock 83.84 s → **95.84 s** (+14 %, within 20 % gate). Pyright 0/0/0, ruff clean — maintained continuously. 227 pre-v1.2 spec corpus tests bit-identical. Bot regression zero impact across the entire branch work; trader_worker continuous **30 h+** uptime during branch development, deliberate post-merge rebuild preserved warmup state.

- **Cost-sanity check before extraction — gate against structurally-unviable sources.** Hunt 6B (2026-05-25, post-teaching-fix re-run) is the type specimen: a pure-session strategy (long 22:00–23:00 UTC every day) extracted cleanly, ran through the full pipeline, and the gauntlet correctly verdicted `likely_overfit` 73.44/100 — but the actual failure mode was **structural cost-eating**, not edge skepticism. The strategy fired **851 round-trip trades** over 2.3 years at ~30 bps round-trip (10 bps commission both sides + 5 bps slippage both sides) = ~255 % of capital paid in fees alone. Source-claimed edge was ~0.07 %/hour, which is dwarfed by realistic fills. The gauntlet's job is to refuse bad strategies, but spending $0.15 of extraction credit + ~5 min of gauntlet time on a structurally-unviable source is wasted compute we can avoid.

  **Standing pre-extraction rule (back-of-envelope, no precise math required):** before submitting a hunt's raw_text for extraction, if the source implies > ~200 trades / year at 1H or higher frequency on crypto venues, compute:

  ```
  edge_after_costs = claimed_annual_return_pct − (trades_per_year × round_trip_cost_bps × 1e-4 × 100)
  ```

  If `edge_after_costs ≤ 0` or marginal, **do NOT extract.** Log the rejection in the hunt source ledger with the calculation. Save the $0.15 extraction credit and the gauntlet time.

  Per-venue round-trip cost reference for the back-of-envelope calc (post-Phase-C this list grows):

  | venue | round-trip cost (bps) | source |
  |---|---|---|
  | binance_spot BTC/USDT (taker × 2 + slippage × 2) | ~30 | B.1 + B.2 default tables, validated |
  | binance_spot ETH/USDT | ~30 | B.1 + B.2 default tables |
  | (Phase C C.1+) FX majors EUR/USD | ~1-4 | Oanda spread tables, TBD |
  | (Phase C C.8) XAU/USD | ~24 | Wider gold spreads, TBD |
  | (Phase C C.9) equity ETFs SPY/QQQ | ~4 | Alpaca + bid-ask, TBD |

  **Phase C generalisation:** FX venues have ~10× lower round-trip than crypto (~1-4 bps for majors) but tick-level / scalp strategies push `trades_per_year` into the thousands. **Same rule, inverted parameters.** A 5000-trade/year FX scalp at 2 bps round-trip costs (5000 × 2 × 1e-4 × 100) = 100 % of capital in fees per year — same structural failure as Hunt 6B's crypto-1H seasonal. Apply per-venue cost assumption.

  **This is a pre-extraction gate, not a post-gauntlet rejection.** The point is: (a) save the $0.15 extraction credit on structurally-unviable sources; (b) prevent extraction-pipeline-tested "false negatives" that are actually structural rejections (a clean `likely_overfit` gauntlet verdict looks identical for "no real edge" vs "edge exists but cost-eaten" — the back-of-envelope distinguishes them before paying for the test).

- **DSR frequency-pairing rule — caller-side fix at `overfitting_analysis.py:120–138`, 2026-05-25.** Audit of the gauntlet's deflated Sharpe surfaced a caller-side frequency mismatch: `metrics.sharpe_ratio` (ANNUALIZED in `metrics.py:169` via `sharpe = mean_r * bpy / (std_r * sqrt(bpy))`) was passed alongside `metrics.bars_processed` (raw BAR count) as `n_observations`. The Bailey & López de Prado 2014 PSR/DSR formula requires both at the same frequency. The mismatch inflated `sqrt(T-1)` by `sqrt(bpy)` ≈ **47× on 4H**, ≈ **112× on 1H**, ≈ **187× on 15m**, pegging `prob_real ≈ 0` for every strategy in all 15 stored historical gauntlet runs (observed Sharpes spanning −9.13 to +1.04). **Function was correct; caller was wrong.**

  **The fix:** `t_years = max(metrics.bars_processed / metrics.bars_per_year, 2.0)`. The crucial sourcing detail is reading `bars_per_year` directly off the `BacktestMetrics` schema field (set at `metrics.py:115` alongside the Sharpe at line 96) — guaranteeing the EXACT bpy that annualized the Sharpe is the same one denominating T. Re-importing `bars_per_year()` and re-looking-up the timeframe would re-introduce the same class of frequency bug if metrics.py's convention drifted independently. **Trust the producer; read from the schema.**

  **Frequency-pairing rule (generalises beyond DSR):** any statistical test that takes (estimator, sample_size) must pair them at the same frequency. Sharpe-ratio confidence intervals, Newey-West standard errors, autocorrelation-adjusted t-tests, bootstrap-of-the-Sharpe — all require the same care. **Phase C onward, any new gauntlet test added must explicitly document its frequency convention at the call site.**

  **Historical impact assessment:** every stored `prob_real = 0.0` across 15 hunts is a frequency-bug artefact, not a discrimination result. Corrected `prob_real_v2` lives in `workers/data/dsr_backfill_v2.json` keyed by analysis_id, produced by `workers/scripts/backfill_dsr_v2.py` (idempotent). **Original `prob_real` column in `overfitting_analyses` deliberately UNCHANGED** for traceability; composite scores and seed decisions UNCHANGED.

  **Composite invariance:** due to the non-linear mapping in `composite.py:_deflated_sharpe_contribution` (where `prob_real ∈ [0, 0.5)` all map to 60–100 pts), the bug had effectively zero impact at MarketMind's current Sharpe range (0.67–1.04). Modern Turtle's `prob_real_v2` is 0.003 (composite contribution moves by < 0.05 pts; verdict still `likely_robust`). The bug WOULD have mattered for hypothetical SR > 2 strategies — flagging this as the **"C.7 first FX seed risk"** the audit identified.

  **Standing rule for Phase C onward:** review `prob_real_v2` from the sidecar JSON, not `prob_real` from the DB column, when evaluating gauntlet runs. Original `prob_real` retained for historical traceability only. When the next gauntlet run is executed post-fix, both the DB and sidecar will agree (since the caller now uses the correct conversion); only historical runs need the sidecar lookup.
