# MarketMind AI — Project Context

> Operating rules only. Full phase history, hard-won knowledge, completed-phase build notes, and the full doc index live in **`docs/project_log.md`** (searchable, not auto-loaded). Split out of this file 2026-06-04 to stay under the 40k-char perf threshold.

## North star
A research tool that tells users whether trading strategies actually work, 
using rigorous backtesting and overfitting detection. NOT a trading bot. 
NOT a signal service. NOT a portfolio manager.

## Current state
- **v1.2 COMPLETE** — merged + tagged `v1.2.0-final` at `913994a` (2026-05-25). Phases 2–4, 5.1, 5.2a, Phase A, Phase B, v1.2 all done. Next open work: opportunistic strategy hunting; Phase C (multi-asset, deferred); Phase 5.2b (Railway deployment). See `docs/project_log.md` for the full phase narratives.
- **Paper bot:** 7 strategy versions approved+enabled in paper, all BTC/USDT (6×4H + 1×1H). **Modern Turtle Donchian Breakout 4H** (first v2-native `template='spec'`) is past its 255-bar warmup and live-evaluating as of 2026-06-04; the four most recent (hunt-seeded) strategies are warming up. No entry signal has fired yet. `TRADER_SYMBOLS="BTC/USDT,ETH/USDT"`, `TRADER_TIMEFRAMES="4h,1h,15m"`. Daily summary → `data/daily-summaries/` at 00:05 UTC.
- **Deployment env vars** for Phase 5.2b (Railway): `NEXT_PUBLIC_PLAUSIBLE_DOMAIN`, `ADMIN_USERNAME`/`ADMIN_PASSWORD`, `DAILY_COST_CAP_GBP`/`GBP_USD_RATE`. Full catalogue in `/docs/deployment/env-vars.md`.
- **Phase E (2026-06-05, branch `v2-phase-e-perp-pairs`):** unlocking the research/backtest path for multi-leg / market-neutral / short / perpetual-swap shapes (fast directional edge proven dead across 3 gauntlet tests). **E.1** (constraint rewrite — see *Research freedoms* below) + **E.2** (BTC/ETH perp OHLCV + mark + 8h funding data layer, Binance USDM) DONE; **E.3** (multi-leg specs + perp-aware accounting + spread primitives) is next. Equity ORB is CLOSED (REJECT, no edge — branch `v2-phase-d-equities-orb`); the 3 session-anchored primitives live only there and are cherry-pickable if a future phase needs DST-aware time gating. The live trader is untouched and stays spot-long-only.

## Engineering invariants
- Type everything. No `Any`, no `any`.
- Tests are not optional.
- The backtester must never lie. Fees, slippage, realistic fills from day one.
- LLM output is never trusted blindly. Validate against schema, surface uncertainty.
- Solo-dev scale. No Kafka, no Kubernetes, no premature abstraction.

## What we deliberately do not build
- Authentication (not until phase 5)
- Live trading (not for years — a paper-trading bot exists as Phase A's deployment surface, with `assert_paper_only()` as a hard runtime guard that aborts trader boot on any non-paper value of `TRADER_ALLOW_LIVE`)
- Reddit/Twitter sentiment (low-edge, deliberately excluded)
- Autonomous trade execution (not in scope)

## Research freedoms — strategy shapes the RESEARCH/BACKTEST path may now express (Phase E, 2026-06-05)

Long-only / single-asset / spot-only / no-shorting were **conservative defaults, never requirements.** Three independent gauntlet tests — crypto fast trend, crypto fast mean-reversion, equity ORB (all REJECT, ~0 edge) — proved fast **directional** edge does not survive our rigor. Edge lives in slow trend (our only seeds) **or** in non-directional / relative-value strategies those defaults made inexpressible. Phase E lifts the defaults **for the PAPER RESEARCH / BACKTEST path only.** First target: BTC/ETH perpetual-pair spread mean-reversion (a genuinely fast, market-neutral edge).

**NEWLY ALLOWED on the research/backtest path:** multi-leg positions, market-neutral / relative-value spreads, **short** legs, and **perpetual-swap** instruments (with funding + mark price). `Direction` already carries `SHORT`; the rest is additive schema/engine work owned by **Phase E.3**.

**EXPLICITLY UNCHANGED — these are the SAFETY WALL, not strategy-shape defaults. They are NOT what we are relaxing:**
- **`assert_paper_only`** — the hard runtime guard that aborts trader boot on any non-paper `TRADER_ALLOW_LIVE`. UNTOUCHED. It is about paper-vs-live, NOT strategy shape.
- **No-LLM-in-the-trading-path** — the live trader never calls an LLM. UNTOUCHED.
- **Gauntlet rigor** — every overfitting threshold stays exactly where it is. A market-neutral spread must clear the SAME bar as a directional strategy. UNTOUCHED.
- **The live trader stays spot-long-only.** Only the research/backtest path gains the new shapes. This boundary must stay clean (see the table).

**Standing risk rule — market-neutral pairs are HEDGED, not SAFE.** The primary loss mode is **spread divergence** (the legs drift apart, not back together); the tail risk is **regime decoupling** (the historical linkage breaks — e.g. an ETH-specific event). Mitigations: start with **BTC/ETH** (the most tightly-linked liquid pair); and **NO LEVERAGE in paper research until explicitly chosen** — unlevered keeps liquidation dormant (the no-leverage `sizing.percent ≤ 1.0` wall stays). "Hedged" removes directional beta, not basis risk.

**The live-trader safety boundary (must stay clean — live = spot-long-only, research = free).** The live-trader walls STAY; the research-path defaults are what E.3 relaxes. Do not cross them:

| enforcement | file:line | path | disposition |
|---|---|---|---|
| SpecTemplate rejects `direction != LONG` | `trader/templates/spec_template.py:99` | LIVE | **KEEP — safety wall** |
| Risk manager blocks SELL signals | `trader/risk.py:170` | LIVE | **KEEP — safety wall** |
| No leverage (`sizing.percent ≤ 1.0`) | `schemas/strategy_spec/sizing.py:20` | shared | **KEEP — safety wall** |
| One OPEN position per (version, symbol) | DB UNIQUE, `schemas/trader.py` | LIVE | **KEEP — DB structural** |
| Homogeneous asset class per deployment | `trader/config.py:120` | LIVE | **KEEP — operational gate** |
| Tier-3 iterative engine `direction == LONG` only | `backtest/iterative.py:361` | RESEARCH | E.3 relaxes (route shorts to vbt / add short Tier-3) |
| `AssetClass` has no perp value | `schemas/strategy_spec/common.py:77` | shared | E.3 adds `crypto_perp` |
| `CostModel` has no funding field | `schemas/strategy_spec/costs.py:34` | shared | E.3 adds optional funding accrual |
| `instrument: Instrument` is singular | `schemas/strategy_spec/spec.py:80` | shared | E.3 makes multi-leg expressible (biggest change) |

**Phase E.3 must build:** the multi-leg spec shape (lift the singular `instrument`), perp-aware position accounting (funding accrual on **mark** price, mark-vs-last PnL — see the perp fixtures), a `crypto_perp` asset class + perp cost model, and cross-asset/spread primitives — all on the research/backtest path, **with the live trader untouched.** Perp data fixtures: `tests/fixtures/market/binance_{btc,eth}_usdt_perp_1h.parquet` (last OHLCV + `mark_close`) and `..._funding.parquet` (8h funding), Binance USDM via `workers/scripts/fetch_perp_fixture.py` (public, no keys).

## Engineering & trading rules

Rules that change future behavior or prevent bugs. The history and the lessons behind each one live in `docs/project_log.md`.

**State & data integrity**
- **`have_bars` / warmup count is computed from `trader_candles` each cycle, never stored.** State and warmup are restart-safe by construction — a container rebuild or host sleep does not lose warmup (proven across the 2026-05-25 rebuild and the 2026-05-30 → 06-04 sleep blackout).
- **`trader_candles` is append-only — never renumber bars.** T3 `SignalHistory` uses absolute bar indices into `trader_candles` for a symbol+timeframe; ingestion enforces append-only via `INSERT ... ON CONFLICT (symbol, timeframe, open_ts) DO NOTHING`. Any migration that compacts or renumbers candles MUST remap `SignalHistory` indices in `trader_strategy_state`.
- **15m candle series has an unrepaired ~2.85-day gap (2026-05-30 11:30 → 06-02 08:15)** from the June-sleep backfill hitting its 200-bar fetch cap. `ON CONFLICT DO NOTHING` will NOT self-heal it. **Deep-backfill 15m before seeding any 15m strategy.** 4H and 1H are contiguous and unaffected.
- **Cadence boundary: 4H = strong, 1H = edge-case, 15m = not seedable for trend strategies.** Lower TFs amplify cost drag and noise. Also note `_MIN_WINDOW_BARS = 200` on SpecTemplate — even a no-indicator spec needs 200 closed candles before first evaluation (4H ≈ 33 days, 1H ≈ 8 days, 15m ≈ 2 days).

**Schema, specs & evaluation**
- **`spec_uses_stateful_v2(spec)` is the state-aware gate, NOT `schema_version == "2.0"`.** The LLM can declare 2.0 while actually writing a Tier-1 strategy. All state-aware code (overfitting weights, candle backfill depth, SpecTemplate runtime branches) uses the actual-use check.
- **Production constructs specs via `validate_spec` / `model_validate`, never positional `Instrument(...)`** (or any positional model construction). Validation must run on the path that reaches the engine/trader.
- **`WeekdayFilter` uses ISO weekdays; `DayOfWeekCondition` uses pandas Mon=0..Sun=6.** Two different conventions — do not cross them.

**Backtest honesty & the gauntlet**
- **Confirmation-layered entries pass the gauntlet; single-signal entries fail.** A real edge survives multiple independent confirmations; a lone trigger is noise. Consistent across the hunt era — gauntlet rejection is the system working, not a bug.
- **Cost-sanity gate BEFORE extraction.** Before submitting a hunt's raw_text, if the source implies > ~200 trades/year at 1H+ frequency on crypto venues, compute:
  ```
  edge_after_costs = claimed_annual_return_pct − (trades_per_year × round_trip_cost_bps × 1e-4 × 100)
  ```
  If `edge_after_costs ≤ 0` or marginal, **do NOT extract** — log the rejection with the calc, save the ~$0.15 + gauntlet time. FX inverts the parameters (~1–4 bps round-trip, but scalps push trades/year into the thousands → same structural failure). Per-venue round-trip reference:

  | venue | round-trip (bps) | source |
  |---|---|---|
  | binance_spot BTC/USDT | ~30 | B.1+B.2 tables, validated |
  | binance_spot ETH/USDT | ~30 | B.1+B.2 tables |
  | FX majors EUR/USD (Phase C) | ~1–4 | Oanda spreads, TBD |
  | XAU/USD (Phase C) | ~24 | gold spreads, TBD |
  | equity ETFs SPY/QQQ (Phase C) | ~4 | Alpaca + bid-ask, TBD |

- **DSR / statistical tests: T in years, not bars.** `t_years = max(metrics.bars_processed / metrics.bars_per_year, 2.0)`, reading `bars_per_year` **off the `BacktestMetrics` schema field** (the same one that annualized the Sharpe) — do not re-derive it. General **frequency-pairing rule:** any test taking (estimator, sample_size) must pair them at the same frequency; document the frequency convention at every gauntlet call site. For historical runs read `prob_real_v2` from the sidecar `workers/data/dsr_backfill_v2.json`, not the original `prob_real` DB column (retained for traceability only).

**Engine & test discipline**
- **Drift parity has two shapes — pick per primitive.** *Cadence parity* (incremental `iterative_live.py` vs one-shot `iterative.py`, bit-identical) for stateful Tier-2/Tier-3. *Cross-engine envelope* (vbt vs iterative trade-count within ±2×, plus a dispatcher-level `assert_series_equal`) for purely-additive primitives where strict ledger identity is impossible by construction. Wrong shape = ~1 hr debugging a gate that can never go green.
- **Empirical-inspection before encoding numeric assertions.** Any test asserting on `entry_*` / `exit_*` / `fill_*` values: RUN once with prints → DOCUMENT the actual engine output in the docstring → THEN encode against actual values. (E.g. the iterative engine reports `entry_time` as the SIGNAL bar's open, not the fill bar's.) Mandatory for exit primitives, new dispatcher branches.
- **Pyright is the completeness oracle for discriminated-union extensions.** Adding a variant to a Pydantic discriminated union (Condition / Expression / TakeProfitMethod) breaks pyright exhaustiveness on every downstream dispatcher. Run pyright BEFORE writing tests to inventory the branches needed — schema + engine plumbing fold into one commit. Use keyword-only-with-default (`*, x: T | None = None`) for any backward-compatible signature widening.

**Extraction & deployment**
- **Editing `EXTRACTION_SYSTEM_PROMPT` byte-for-byte invalidates the Anthropic prompt cache.** The design doc (`/docs/extraction-prompt.md`) and the constant (`workers/src/marketmind_workers/services/extraction_prompt.py`) must stay in lockstep. Verify `cache_read_input_tokens` is non-zero on the second extraction in a session before declaring the cache live. When fixing one primitive's prompt teaching, audit related primitives in the same pass (a degenerate or missing worked example silently teaches the wrong thing).
- **Container-rebuild discipline: after any schema / indicator-whitelist / v2-primitive change, rebuild all three Python containers (`api`, `worker`, `trader_worker`) with `--no-deps`** (~6–8 min back-to-back). `--no-deps` avoids recreating `web`/`postgres`/`redis` as side-effects. Rebuilds are safe for the running bot — warmup is reconstructed from `trader_candles`, not in-memory state.

## Canonical sources of truth
- `/docs/strategy-spec.md` — strategy spec v1.0; canonical source for the schema. Pydantic models, validation, executor, UI all derive from it. The bounds table is mirrored in `shared/src/marketmind_shared/schemas/strategy_spec/indicators.py` — **any change in one requires a matching change in the other.**
- `/shared/src/marketmind_shared/schemas/strategy_spec/` — executable form of the spec (common/expressions/indicators/conditions/entry/exit/sizing/filters/costs/metadata/spec/validator/errors).
- `/workers/src/marketmind_workers/services/extraction_prompt.py` (`EXTRACTION_SYSTEM_PROMPT`) — the extraction prompt; cache-lockstep rule above.
- `/workers/src/marketmind_workers/backtest/iterative.py` + `iterative_live.py` — Tier-3 batch simulator + sibling live stepper; drift-parity zero divergence is the load-bearing CI gate.
- `/workers/src/marketmind_workers/trader/templates/spec_template.py` — generic SpecTemplate; new strategies route here. The v1 hand-coded templates (`bb_mean_reversion`, `breakout`, `ma_trend`, `rsi_mean_reversion`, `vcb`) are kept for pre-Phase-A seeds.
- `/workers/src/marketmind_workers/backtest/fee_model.py` + `slippage_model.py` — engine reads costs through these, not `spec.costs` (decorative since B.2).
- Migrations `0012_trader_v2_spec_template.sql`, `0013_v2_trader_strategy_state.sql` — v2 schema (SpecTemplate routing column + `trader_strategy_state` table).
- Full design-doc / operations-guide index: `docs/project_log.md` § "Sacred design artifacts — full index".

## Editorial Quant design system (Phase 5.1 — LOCKED)

The frontend design language is **Editorial Quant**: a quant tool that reads like a Financial Times op-ed. Serif display typography, restrained warm palette, hairline borders not shadows, near-zero motion, asymmetric editorial layouts. NOT a SaaS aesthetic. Reference points: Pudding.cool, NYT Upshot, Stripe Press, FT data journalism. Anti-references: Linear, Bloomberg Terminal, generic SaaS marketing sites.

**LOCKED tokens — do not extend without a design ADR.** Tailwind config + globals.css are the source of truth (`web/tailwind.config.ts`, `web/src/app/globals.css`).

Colours (CSS variables):
- `--color-bg` `#FAF8F3` (warm cream page background)
- `--color-surface` `#FFFFFF` (cards, table rows)
- `--color-ink` `#1A1815` (body text + chart primary)
- `--color-muted` `#5C5852` (secondary text, axis labels)
- `--color-hairline` `#E5E1D8` (every 1px border)
- `--color-accent` `#8B3A1F` (burnt-sienna; primary buttons, chart B&H line, accent verdicts)
- `--color-positive` `#2D5A3D` (forest; "beat benchmark", positive returns)
- `--color-negative` `#8B2C2C` (oxblood; "underperformed", refusal callout border, negative returns)
- `--color-fill` `#F0EBE0` (subtle fill for hover + code blocks)

Fonts (next/font Google):
- **Source Serif 4** — all `h1`/`h2`/`h3` and display copy. Weights 400, 700.
- **IBM Plex Sans** — body + UI. Weights 400, 500, 600. Available as `font-sans`.
- **IBM Plex Mono** — every numeric in tables, KPI cells, axis labels, code. Weights 400, 500. Available as `font-mono`. Always paired with `tabular-nums`.

Type scale: 1.250 (major third). `text-xs` through `text-4xl`. Hero numbers (overfitting score, alpha) use `text-4xl`. Page heads `text-3xl/4xl`. Section heads `text-2xl`.

Spacing: multiples of 4px. Sections separated by `Separator` + 56–96px vertical padding.

Borders: ALL bordered elements use 1px solid `var(--color-hairline)`. No `box-shadow`. Border radius capped at 4px (default `rounded-sm` = 2px). No pill buttons, no fully-rounded cards.

Numeric display: every number (table cell, KPI value, axis tick) renders in IBM Plex Mono with `tabular-nums`. Use `.num` class or `<TableCell numeric>`. This is load-bearing for visual alignment.

Component primitives in `web/src/components/ui/` (shadcn-style with cva + Radix):
- `Button` — `intent: primary | secondary | ghost`, `size: sm | md | lg`. Hover transitions colour only (no transforms).
- `Card`, `CardHeader`, `CardTitle`, `CardEyebrow`, `CardContent` — hairline border + cream/white surface.
- `Table` family — applies the `editorial` CSS class for hairline dividers + tabular-num numerics.
- `Badge` — square corners, hairline border, eyebrow type. Intents: neutral / accent / positive / negative.
- `Separator` — Radix-backed hairline rule.
- `Skeleton` — placeholder rectangle, no shimmer animation.

Chart palette (Recharts): ink (#1A1815), accent (#8B3A1F), positive (#2D5A3D), negative (#8B2C2C). No rainbow palettes. No gridlines (`CartesianGrid horizontal={false} vertical={false}`). Hairline axes (`stroke: #E5E1D8`). Mono tabular axis ticks (`fontFamily: var(--font-ibm-plex-mono)`).

Motion: near-zero. `transition-colors` only on hover (no scale, translate, rotate). No page-load fade-ins. Animations are reserved for genuine state changes (e.g., a job-progress poller) and even then must be discrete.

Banned (do not reintroduce):
- Inter, Roboto, Arial, system-ui sans
- Purple, sky-blue, neon palettes, multi-colour chart rainbows
- Box shadows of any kind
- Border radius > 4px (no pills, no fully-rounded cards)
- Hover transforms (scale/translate/rotate)
- Emoji icons (Lucide React or hand-drawn SVG only)
- Bento grids, SaaS hero gradients, "modern SaaS" tropes

**Future sessions: respect these tokens. New pages should compose from the existing primitives + `eyebrow` / `callout` / `editorial` CSS helpers. Adding a colour, font, or shape language requires explicit user approval and a CLAUDE.md update.**

## Dark theme — Honest Terminal (Phase 5.1b — LOCKED, DEFAULT)

The dark sibling of Editorial Quant. Same restraint, same philosophy, inverted canvas. Reads like a hedge-fund risk dashboard built with care — NOT cyberpunk, NOT Linear-style cool-grey, NOT pure black. **Dark is the default for new visitors** (set by the FOWT-prevention inline script in `app/layout.tsx`); the user's last toggle wins on subsequent visits via `localStorage("marketmind-theme")`.

Tailwind is configured `darkMode: 'class'`. Everything switches by toggling the `.dark` class on `<html>`.

Tokens (CSS variables under `.dark` in `globals.css`):
- `--color-bg` `#0E0E0C` (warm near-black canvas — NOT pure #000)
- `--color-surface` `#16161A` (cards, table rows)
- `--color-ink` `#E8E4D8` (warm off-white text — NOT pure #FFF)
- `--color-muted` `#8A8478` (muted body)
- `--color-hairline` `#2A2826` (every 1px border)
- `--color-accent` `#D49A3A` (warm amber; primary buttons, B&H line, accent verdicts)
- `--color-positive` `#5FA572` (muted forest; positive verdicts)
- `--color-negative` `#C5564A` (muted oxblood; negative verdicts)
- `--color-fill` `#1F1E1B` (hover, code background)

Typography, spacing, border radii, motion policy — **identical** to the light theme. Only colours change.

ThemeToggle (`src/components/theme-toggle.tsx`): Lucide Sun/Moon icon, hairline-bordered Button, in the top-right of every redesigned page header. No animation on the swap — instant switch, consistent with the near-zero-motion rule.

FOWT-prevention: `src/lib/theme-script.ts` exports a one-line synchronous IIFE that the root layout injects into `<head>` via `dangerouslySetInnerHTML`. It reads `localStorage("marketmind-theme")`, falls back to `prefers-color-scheme: light` (= apply nothing) else applies `.dark`. Runs before paint to avoid flash of wrong theme. `<html suppressHydrationWarning>` silences the unavoidable React mismatch the script causes.

Chart colours (Recharts): the `useThemeColors()` hook in `src/lib/use-theme-colors.ts` reads the current CSS-variable values via `getComputedStyle` and re-reads whenever `<html>`'s class changes (via MutationObserver). Charts pass the resolved hex strings into Recharts props so they re-render correctly on every theme switch.

Dark-theme banned (do not reintroduce):
- Electric blue / cyan accents — that's Linear / Vercel cliché
- Pure black background (#000) — we use warm `#0E0E0C`
- Pure white text (#FFF) — we use warm `#E8E4D8`
- Bright neon green / red — our positive/negative are muted, mature
- Cool-grey palette — our dark is WARM, not cold
- Drop shadows, glow effects, luminance fakery
- Animated theme transitions — `transition-colors` etc. is banned on the theme swap; charts and surfaces flip instantly
