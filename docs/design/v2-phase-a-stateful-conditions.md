# v2 Phase A — Stateful Conditions: Design

**Status:** Design draft for review. No implementation. Phase 0 deliverable.
**Branch:** `v2-phase-a-stateful-conditions` (from `main` @ `9e64326`).
**Gate:** No Phase A code until this document is explicitly approved.
**Author:** Phase 0 design pass, 2026-05-20.

---

## 0. Summary and the central finding

Phase A aims to let MarketMind extract, backtest, overfitting-test, and
paper-trade strategies whose logic is **path-dependent** — the decision at bar
*N* depends on what happened at bars `0..N-1`, not just the current window.

### 0.1 The central finding: "stateful" is three different problems

The most important conclusion of this design pass is that the requested
features are **not one homogeneous capability**. They split into three tiers
of increasing architectural cost. This taxonomy drives every section below.

| Tier | Definition | Example | Engine cost |
|------|-----------|---------|-------------|
| **T1 — bounded-window** | State with a fixed finite horizon; expressible as a `rolling`/`shift`/`ewm` op | "RSI was oversold within the last 5 bars" | **None** — already vectorised; precedent exists |
| **T2 — unbounded, input-dependent** | A recurrence `s[t]=f(s[t-1], inputs[t])` over price/indicator inputs, no fixed window | regime latch; ratchet of "highest close since start" | **Low** (revised in A.3a) — vectorised pandas: `cummax`/`cummin`, `ffill` latch; feeds `from_signals` unchanged |
| **T3 — unbounded, outcome-dependent** | A recurrence over *trade results*, which do not exist until the backtest has run | skip-after-winner; "stop trading after 3 consecutive losses" | **High** — breaks vectorbt's `from_signals` model entirely |

### 0.2 Honest headline — what already exists

Two of the four requested "unlocks" are **partly or fully already in v1**:

- **Trailing stops already exist.** `StopLossTrailingPercent` and
  `StopLossTrailingAtr` are v1 schema (`exit.py:39-47`); the engine executes
  them through vectorbt's native `sl_trail=True`
  (`engine.py:338,344`). Phase A does **not** need to add trailing
  *stop-losses*. What Phase A adds is a **general `ratchet` primitive** usable
  inside *any* condition (entry filters, exit conditions), of which the
  trailing stop is one narrow special case.
- **N-bar lookback state already exists.** `WithinLastNBarsCondition`
  (`conditions.py:34`), `RisingCondition`, `FallingCondition` already give
  bounded-window state, fully vectorised via `.rolling()`/`.shift()`
  (`translator.py:534-560`). This is Tier 1 and it is shipped. Phase A may
  *extend* it, but the primitive is not new.

The genuinely new work is **regime flips (T2)** and **skip-after-winner (T3)** —
and T3 is the one that does not fit the current architecture.

### 0.3 Honest headline — the Turtle tension

The chosen integration test, **Turtle Trading**, is *specifically* a T3
strategy: the original Turtle System-1 entry rule is "take the 20-day breakout
**unless the previous breakout would have been a winner**." That is
skip-after-winner — outcome-dependent state. **The Turtle acceptance criterion
therefore cannot be met without doing T3.** This forces a scoping decision,
surfaced in §8–§10: either Phase A includes a backtest-engine rewrite (and is
not a single one-shot), or Phase A is scoped to T1+T2 and a different
integration strategy is chosen. This document recommends the latter and
explains why in §9.

---

## 1. Schema changes

The StrategySpec schema lives in
`shared/src/marketmind_shared/schemas/strategy_spec/` (14 modules, 1,485
lines). It is a "sacred artifact": the executable mirror of
`docs/strategy-spec.md`. Conditions and expressions are **Pydantic v2
discriminated unions** keyed on a literal tag (`type` for conditions, `kind`
for expressions), resolved with `model_rebuild()` for the recursive variants.

### 1.1 New schema elements

Phase A adds **one Expression variant** and **two Condition variants**. The
fourth requested type, `stateful_compare`, is **deliberately not added** —
see §1.4.

**(a) `RatchetExpr` — a new `Expression` variant (`expressions.py`)**

The general "ratcheting variable that only moves favorably" primitive. It is
an *expression*, not a condition, so it composes into `compare`/`crossover`
exactly like any other expression — a trailing-stop entry filter is simply
`compare(close, ">", ratchet(...))`.

```python
class RatchetExpr(_StrictModel):
    kind: Literal["ratchet"] = "ratchet"
    source: Expression                       # the value being ratcheted
    extremum: Literal["max", "min"]          # ratchet up (max) or down (min)
    reset: Literal["never", "per_trade"] = "per_trade"
```

Semantics: at each bar, `ratchet` equals the running `max` (or `min`) of
`source` since the last reset. `reset="never"` runs over the whole series
(T2 — clean). `reset="per_trade"` resets at each position entry (see §4.4 —
this re-introduces an ordering dependency and is the harder case).

**(b) `RegimeStateCondition` — a new `Condition` variant (`conditions.py`)**

A latched boolean — the "Supertrend direction state" / regime-flip primitive.

```python
class RegimeStateCondition(_StrictModel):
    type: Literal["regime_state"] = "regime_state"
    enter_when: Condition        # latch ON when this is true
    exit_when: Condition         # latch OFF when this is true
    initial: bool = False        # state before the first trigger fires
```

Semantics: TRUE from the bar `enter_when` first fires until the bar
`exit_when` fires, then FALSE until `enter_when` fires again. This is a pure
function of price/indicator inputs → **T2**.

**(c) `PriorTradeCondition` — a new `Condition` variant (`conditions.py`) — T3**

```python
class PriorTradeCondition(_StrictModel):
    type: Literal["prior_trade"] = "prior_trade"
    predicate: Literal[
        "last_won", "last_lost",
        "consecutive_losses_at_least", "consecutive_wins_at_least",
    ]
    n: int = Field(default=1, ge=1, le=100)   # used by consecutive_* only
```

This is **T3** and is included in the schema for completeness, but §4/§8/§9
recommend it ship in a separate sub-phase — it cannot be evaluated by the
current backtest engine.

### 1.2 Wiring the unions

`RatchetExpr` joins the `Expression` union (`expressions.py:67`); it wraps an
`Expression`, so it needs `RatchetExpr.model_rebuild()` after the alias —
mirroring `LaggedExpr`/`ScaledExpr` (Phase 1 hard-won knowledge:
forward-referencing union members must be rebuilt). `RegimeStateCondition`
wraps `Condition` (twice) and `PriorTradeCondition` is a leaf; both join the
`Condition` union (`conditions.py:87`) and `RegimeStateCondition` needs
`model_rebuild()`.

**The discriminated unions are additive — this is what makes §1.3 work.**
Adding a new `kind`/`type` variant cannot change how an existing spec parses:
a v1 spec contains none of the new tags, so Pydantic routes it to exactly the
same variants as before.

### 1.3 State representation and lifecycle

A key design choice: **the schema declares stateful conditions; it does not
hold their state.** State (a ratchet's current extremum, a regime's latch
flag) is *runtime* data. It lives in two places, both outside the schema
package:

- **Backtest:** computed by vectorised pandas ops at translate time (§4).
- **Live trader:** persisted in a new `trader_strategy_state` table (§6).

Runtime state is typed (the project bans `Any`) with small Pydantic models
kept in `shared/src/marketmind_shared/schemas/trader.py` next to
`PaperPosition`:

```python
class RatchetState(BaseModel):
    current_extremum: float | None = None
    reset_at_ts: datetime | None = None

class RegimeState(BaseModel):
    latched_on: bool
    last_flip_ts: datetime | None = None
```

**Lifecycle:**
- *Initialize* — at strategy start (backtest `start`, trader first cycle):
  `RatchetExpr` extremum = the first `source` value; `RegimeState.latched_on`
  = `RegimeStateCondition.initial`.
- *Update* — per bar, via the recurrence.
- *Reset* — `ratchet(reset="per_trade")` resets at each entry;
  `reset="never"` and `regime_state` never reset (a market regime is not a
  per-trade thing). `regime_state` is deliberately *not* reset on trade
  boundaries — that is the point of a regime.

### 1.4 Why `stateful_compare` is not added

The requested `stateful_compare` is **subsumed by composition**: a "stateful
comparison" is `CompareCondition` with a `RatchetExpr` operand
(`compare(close, ">", ratchet(close, "max", reset="per_trade"))`). Adding a
dedicated `stateful_compare` type would duplicate `CompareCondition` and give
the extraction LLM two ways to express one thing — which historically
degrades extraction consistency. The schema's existing design philosophy is
"few orthogonal primitives, composed"; `ratchet`-as-expression honours it.
**Recommendation: do not add `stateful_compare`.**

### 1.5 Backward compatibility

`StrategySpec.schema_version` is `Literal["1.0"]` (`spec.py:56`), deliberately
anchored so any other version is rejected "and forces explicit migration via
a v2 schema". Phase A widens it:

```python
schema_version: Literal["1.0", "2.0"] = "1.0"
```

Rules:
- A spec that uses **no** stateful element keeps `schema_version="1.0"` and is
  **byte-identical** to today. All 12 golden fixtures in
  `tests/fixtures/strategies/` parse unchanged and (acceptance criterion, §10)
  produce **identical backtest results**.
- A spec that uses any v2 element **must** declare `schema_version="2.0"`
  (validator rule, §2). This keeps the version field meaningful and lets the
  extraction prompt / UI branch on it.
- The export pipeline (`shared/scripts/export_json_schema.py` →
  `web/src/types/generated/schemas.json`) regenerates with the new variants;
  the TypeScript bundle and the extraction tool schema (§3) follow.

No existing validator rule, sub-model, or default changes. This is a purely
additive schema change — the lowest-risk shape available.

---

## 2. Validator changes

`validate_spec(data)` (`validator.py:39`) is the single boundary that turns
untyped JSON into a typed `StrategySpec`. It returns `(spec, warnings)` or
raises `StrategySpecValidationErrorGroup`. Stable `error_code` slugs come from
`PydanticCustomError("slug", "message", {params})` — the pattern exemplified
by `scaled_factor_zero` / `scaled_factor_out_of_bounds` (`expressions.py:46-61`).

### 2.1 New validation rules

All new rules raise `PydanticCustomError` so they surface as stable
`error_code`s through `_convert_errors` (`validator.py:58`), matching the
quality bar of the existing codes.

| Rule | Where | `error_code` | Message |
|------|-------|--------------|---------|
| A v2 element requires `schema_version="2.0"` | `StrategySpec._validate_cross_cutting` | `stateful_requires_schema_v2` | `"{element} requires schema_version '2.0'; spec declares '1.0'"` |
| `ratchet.source` may not transitively contain a `ratchet` | `RatchetExpr` model_validator | `ratchet_nested_unsupported` | `"ratchet.source must not contain another ratchet (nested ratchet semantics are undefined in v2.0)"` |
| `regime_state.enter_when` / `exit_when` may not be a bare `regime_state` of itself | tree walk | `regime_state_trivial` | `"regime_state.{field} must not be the regime itself"` |
| `prior_trade` predicate/`n` consistency | `PriorTradeCondition` model_validator | `prior_trade_n_misused` | `"n is only meaningful for consecutive_* predicates; predicate '{predicate}' ignores n"` (soft warning, not hard error — collected like the direction warnings) |
| Stateful-condition nesting depth bound | tree walk | `stateful_nesting_too_deep` | `"stateful condition nesting exceeds depth {max}; flatten the strategy"` |

The `stateful_requires_schema_v2` check needs a recursive walk of the
condition/expression tree to detect whether any v2 tag is present; this walk
is written once and reused by the diagnostics in §4.5.

### 2.2 Cycle detection — the honest answer

The brief asks for "cycle detection (a stateful condition cannot reference its
own future value)." The honest finding:

**With the inline/anonymous state model chosen in §1.3, cycles are
structurally impossible.** Conditions and expressions form a *tree* (Pydantic
recursive models), not a named graph — there is no way for one condition to
*name and reference* another, so there is no edge that could close a cycle.
Look-ahead (referencing a *future* value) is already prevented by
`LaggedExpr.bars_ago: int = Field(ge=0)` (`expressions.py:37`) — negative lags
are unconstructible.

Cycle detection would only become necessary if v2 introduced **named state
variables** (a `state_variables: dict[str, ...]` block on the spec, referenced
by name elsewhere). That design *can* form cycles (`a = b + 1; b = a + 1`) and
would need a topological sort. **This document recommends against named state
for Phase A** — the requested features (trailing, regime, skip-after-winner)
are all self-contained and inline. So the Phase A validator ships:
1. the structural `stateful_nesting_too_deep` depth bound (defends against a
   hand-crafted or LLM-hallucinated pathological nesting that would otherwise
   stack-overflow Pydantic), and
2. a documented note that true cycle detection is deferred until/unless named
   state is introduced.

This is the honest, minimal, correct treatment — building a topological-sort
cycle detector for a graph shape the schema cannot express would be dead code.

### 2.3 Fixtures

`tests/fixtures/strategies/` holds 8 valid + 4 invalid golden specs, each
invalid one paired with an `.expected_error.json` sidecar. Phase A adds:
- valid: one fixture per new element (`ratchet`, `regime_state`,
  `prior_trade`), `schema_version="2.0"`.
- invalid: one per new `error_code` above, with sidecars.
The existing `test_valid_fixtures` / `test_invalid_fixtures` /
`test_round_trip` / `test_bounds` suites parametrize over the directory and
pick the new fixtures up automatically.

---

## 3. Extraction prompt changes

(Findings grounded in `extraction_prompt.py` and `extract.py`.)

### 3.1 The current state of teaching

- `EXTRACTION_SYSTEM_PROMPT` (`extraction_prompt.py:45`, ~2,200 tokens) teaches
  the schema **by reference**, not by instruction: line ~196 says "The
  StrategySpec schema is provided to you separately" and defers to the tool
  `input_schema`. It contains **zero worked examples** — no example strategy,
  condition, or JSON anywhere.
- The tool `input_schema` (`build_submit_extraction_tool()`,
  `extraction_prompt.py:301`) carries the *structure* of each condition (the
  `type` literal, field names, bounds) but **every condition `$def` has
  `description: None`** — the export script strips Pydantic docstrings. The
  model infers `crossover` vs `rising` purely from the self-descriptive tag
  name plus pretraining.

**Implication:** a new tag like `regime_state` is *less* self-descriptive than
`crossover`. `ratchet` and `prior_trade` even more so. They will extract
poorly with no teaching signal. Phase A must add an explicit teaching channel.

### 3.2 What Phase A changes, and the cache cost

The Anthropic prompt cache key is the byte image of (model + system prompt +
tool definition); both blocks carry `cache_control: ephemeral`.

1. **Tool block — unavoidable one-time cache miss.** New `$defs` for the three
   v2 elements land in the merged top-level `$defs` (the `$defs`-folding
   described in CLAUDE.md). Widening `schema_version` is also a tool-block
   change. This invalidates the tool cache once; accept it as a known
   one-time ~$0.08 write premium (`docs/extraction-prompt.md` already
   documents this expectation).
2. **System prompt — append-only.** Add a new `### STATEFUL CONDITIONS (v2)`
   section **at the very end** of `EXTRACTION_SYSTEM_PROMPT`, after the
   existing "ADDITIONAL RULES" trailer. Appending is the cheapest possible
   prompt edit under prefix caching — it only re-writes the cache once.
   *Do not* insert it mid-prompt.
3. Regenerate `web/src/types/generated/schemas.json`; verify
   `$defs` key ordering is deterministic (sorted) so the tool bytes don't
   drift run-to-run and silently miss the cache every call.
4. After deploy, verify `cache_read_input_tokens > 0` on the second
   extraction — the silent-`cache_control`-typo trap from CLAUDE.md.

### 3.3 The new prompt section — content

The new section is the first place the prompt carries **worked examples**
(a new precedent — every example byte is part of the cache key, so keep them
minimal). It must cover:

- **One worked JSON example per new element** — a `ratchet` trailing filter, a
  `regime_state` latch, a `prior_trade` skip rule, and (added with the
  `prior_signal` extension, §4.7) a `prior_signal` skip-after-winner gate.
- **When to use stateful vs static** — explicit guidance: *prefer static
  conditions*; reach for a stateful element **only** when the source text uses
  language like "trailing", "highest since entry", "until the trend flips",
  "stays long until", "after a winning/losing trade", "skip the next signal".
  A simple SMA cross is *not* stateful — preserving current behaviour for the
  ~majority of strategies is a hard requirement.
- **`prior_trade` vs `prior_signal`** — the prompt must teach the boundary
  (§4.7): `prior_trade` reasons about trades actually taken, `prior_signal`
  about every breakout/signal evaluated, taken or skipped. Turtle System 1's
  skip-after-winner is `prior_signal`.
- **The verdict reminder** — if the strategy's path-dependence cannot be
  captured by the four v2 elements, `PARTIALLY_EXTRACTABLE` (or refuse), do
  not approximate it with a static condition that changes the strategy's
  meaning.

### 3.4 Schema descriptions

A second teaching channel is JSON-Schema `description` strings on the new
`$defs`, co-located with the structure the model fills.

**Correction (A.2):** the export script does *not* "strip" descriptions —
it never did; it is a plain `model_json_schema()` call. The real issue,
found empirically in A.2, is that Pydantic emits a class docstring as the
schema `description` *inconsistently*: leaf models keep it, but recursive
models rebuilt via `model_rebuild()` (`RatchetExpr`, `RegimeStateCondition`)
silently lose it. A.2 fixed this with a post-processing pass in
`export_json_schema.py` (`_inject_model_descriptions`) that injects each
model's docstring summary deterministically, plus `Field(description=...)`
on every field of the new types. Schema descriptions therefore **shipped**
in Phase A — both teaching channels (prompt prose and schema descriptions)
are live.

### 3.5 Quality measurement

Extraction failure on a novel schema is **not** a crash — `extract.py`
retries once, then `_downgrade_to_refusal` forces `NOT_EXTRACTABLE`. So a
prompt regression shows up as a **rising `not_extractable` rate**. The Phase A
rollout must baseline that rate on the existing corpus *before* the change
and watch it after. This is the single most important extraction-quality
signal.

### 3.6 What shipped — the 2026-05-21 hysteresis fix

The first live v2 stateful-seeding attempts surfaced a real gap. Three
extractions of regime strategies (Supertrend, an EMA-200 trend follower, an
RSI-pullback gated by an EMA-200 regime) **all came back Tier-1** — the model
extracted the regime as a stateless `crossover` / `compare` rather than a
`regime_state`. The third was explicit in its `non_backtestable_parts`: the
hysteresis "cannot be expressed in the schema's stateless condition
primitives" — and it used `close > EMA-200` as a "non-latching proxy."

Root cause: the `regime_state` section's only worked example was the
**degenerate same-threshold case** (`enter_when` and `exit_when` both on
EMA-200 — equivalent to a plain `close > EMA-200`). The model generalised
`regime_state` as "a verbose compare" and would not reach for it on a
genuine hysteresis regime.

Fix (`feat(extraction): teach regime_state hysteresis pattern`): the
`regime_state` section was rewritten with (a) a principle line — a
same-threshold regime is degenerate and must **not** be wrapped in
`regime_state`; (b) a genuine **hysteresis** worked example — a
Bollinger-band regime that enters on the upper band and exits on the
middle band, so the latch holds state between the two; (c) a "WHEN TO USE
regime_state" rule — hysteresis, or the regime as a filter on a separate
signal. The change re-writes the Anthropic prompt cache once. Covered by
`test_prompt_teaches_regime_state_hysteresis`.

### 3.7 What shipped — the 2026-05-22 highest/lowest fix + audit pass

A second extraction-prompt gap, same shape as §3.6. A Modern-Turtle Donchian
strategy extracted correctly — the model used `regime_state` for the EMA-stack
hysteresis (§3.6's fix holding) and `highest`/`lowest` for the Donchian
channels — but the spec was rejected at `StrategySpec` validation:
`[indicator_param_missing] highest.source is required` (likewise `lowest`).
`_downgrade_to_refusal` then forced `not_extractable`.

Root cause: `highest`/`lowest` take their price series as a **required
`params` field** (`source`) — the `source_param=True` convention in
`indicators.py` — *unlike* SMA/EMA, which take `source` at the
`IndicatorExpr` top level. The prompt had **no worked example of
`highest`/`lowest` at all**, so the model never learned the distinction.

Fix (`feat(extraction): teach highest/lowest params.source + audit pass`):
a "Highest / Lowest — the Donchian indicators" section was added — the
`params.source` rule (valid values, why it differs from SMA/EMA), and a
worked 20/10-bar Donchian breakout/exit example (`lagged` so the current
bar is not compared against itself). The same commit triggered a full
audit of every primitive's teaching (the §3.6 + §3.7 bug shape recurring
twice motivated a proactive sweep): `regime_state`, `ratchet`,
`prior_trade`/`prior_signal`, `crossover`-vs-`compare` were all found
adequately taught (load-bearing examples); `scaled` was thinly taught
(shown only incidentally inside the `ratchet` example) and gained a
one-line `factor` clarification. Covered by
`test_prompt_teaches_highest_lowest_source`.

---

## 4. Backtest engine changes

(Grounded in `translator.py`, `engine.py`, `backtest_run.py`.)

### 4.1 The current architecture

`translator.build_signals` (`translator.py:172`) walks the condition tree and
emits **whole-series boolean pandas expressions** — there is **no per-bar
Python loop anywhere**. `engine.run_backtest` (`engine.py:97`) is a single
`vbt.Portfolio.from_signals(...)` call; vectorbt walks bars internally in
compiled Numba. Stops are *configured* into vbt
(`sl_stop`, `sl_trail`) and simulated by vbt intrabar.

This is why the tier taxonomy (§0.1) matters: the engine has no place to put
arbitrary per-bar Python state.

### 4.2 Tier 1 — bounded window: zero engine change

Already solved. `WithinLastNBarsCondition` is `inner.rolling(n).max() > 0`
(`translator.py:534`). Any bounded-horizon "state" extension is a new
`_eval_*` helper emitting a `rolling`/`shift`/`ewm` expression. Fully
vectorised, slots into `from_signals` unchanged.

### 4.3 Tier 2 — unbounded input-dependent: vectorised pandas, no numba

`RegimeStateCondition` and `RatchetExpr(reset="never")` are recurrences over
price/indicator inputs.

**Correction (A.3a):** these two do *not* need a numba scan. Each is a
standard vectorised pandas primitive:

- `RatchetExpr(reset="never")` — the running favorable extremum is
  `source.cummax()` (extremum="max") or `source.cummin()` (extremum="min").
- `RegimeStateCondition` — the latched boolean is a marker series (+1 where
  `enter_when` fires, -1 where `exit_when` fires, exit winning a same-bar
  tie) forward-filled, with leading bars taking `initial`. No per-bar loop.

A.3a shipped both as `_eval_ratchet` / `_eval_regime_state` in
`translator.py` — no `workers/backtest/stateful.py`, no `numba` dependency,
no new module. `engine.py` is unchanged; the result is just another
precomputed Series feeding `from_signals`.

The sequential / iterative path *is* genuinely needed — but for **Tier 3**
(§4.6), not Tier 2. A.3b builds it as a custom iterative simulator.

### 4.4 The `reset="per_trade"` ratchet — a hidden Tier-3 edge

A ratchet that resets at *each entry* needs to know entry bars. If the ratchet
feeds an **entry** condition, that is circular (entries depend on the ratchet
depends on entries). If it feeds only an **exit**, entries are known first —
but with multiple sequential trades, trade 2's entry (hence trade 2's ratchet
reset) depends on trade 1's exit. That is iterative.

The trailing **stop-loss** special case escapes this because vbt does the
per-trade trailing internally (`sl_trail=True`). A *general* per-trade ratchet
in a condition does not. **Recommendation:** Phase A ships `RatchetExpr` with
`reset="never"` only (clean T2); `reset="per_trade"` is gated behind the same
engine work as T3 (§4.6). The schema field stays (forward-compatible) but the
validator rejects `reset="per_trade"` with `ratchet_per_trade_not_yet_supported`
until the T3 engine path exists.

### 4.5 Signal diagnostics must extend

The v1.1 `SignalDiagnostics` system (`backtest_run.py:60`,
`translator.py:228`) classifies the *entry* series into
`CONDITIONS_NEVER_MET` / `EVALUATION_DEGRADED` / `NONE`. A stateful condition
introduces a new silent-failure mode: a `regime_state` whose `enter_when`
never fires is FALSE forever; a ratchet whose source is all-NaN poisons every
downstream compare. Phase A adds, mirroring the v1.1 pattern, a
`StatefulDiagnostics` block per stateful element: did the regime ever latch,
how many flips, ratchet min/max range, NaN count. Without this, a degenerate
stateful spec reproduces the exact v1.1 "silent zero trades" incident.

### 4.6 Tier 3 — `prior_trade` / `prior_signal`: breaks `from_signals`

`PriorTradeCondition` depends on trade *outcomes*. Trade outcomes do not exist
until vbt has run. `from_signals` requires all signals precomputed. **There is
no precomputed Series that can express this.** The two real options:

1. **`vbt.Portfolio.from_order_func`** — a per-bar Numba callback that sees the
   running portfolio. The callback can consult prior closed trades. This keeps
   vectorbt but is a substantial new engine path parallel to `from_signals`.
2. **A bespoke iterative simulator** — abandon vbt for stateful specs, walk
   bars in Python/numba maintaining the position and trade ledger.

**Update (A.3b): option 2 shipped.** A.3b built the bespoke iterative
simulator — `workers/backtest/iterative.py::run_iterative_backtest`, backed by
the pure-data `TradeHistory` (`trade_history.py`). `engine.run_backtest` gained
a one-line router: a spec for which `condition_uses_tier3` is true (a
`prior_trade` / `prior_signal` condition, or a `ratchet reset="per_trade"`) is
sent to the iterative path; every other spec stays on the vectorbt path
byte-for-byte unchanged. The A.3a translator keeps `_reject_tier3` as a
defensive guard so a Tier-3 spec can never silently reach the vectorised
engine. The two engines are validated to agree *structurally* — identical
trade count plus entry/exit timestamps — on static specs by
`tests/test_backtest_control.py`.

T3 is therefore **in Phase A scope as shipped**, not deferred — the §9 estimate
that called it "a separate phase" was the design-time recommendation; A.3b
delivered it as a tightly-reviewed sub-phase. The remaining T3 subtlety — that
`prior_trade` cannot express Turtle System 1's skip-after-winner without
latching shut — is resolved by `prior_signal` and phantom outcomes; see §4.7.

### 4.7 `prior_signal` and phantom outcomes

`prior_trade` evaluates against *completed trades*. A.3b's Turtle System 1
integration test surfaced a design gap: an entry gate of
`not(prior_trade last_won)` **latches shut after the first winning trade**.
A skipped breakout opens no trade, so "the most recent completed trade" stays
that first winner forever, and the gate never re-opens. Real Turtle System 1
avoids exactly this — it scores *every* breakout, including the ones it
skipped, as a hypothetical trade. The fix is a second condition type,
`prior_signal`, that refers to the most recent *evaluated entry signal* rather
than the most recent completed trade.

**The condition.** `PriorSignalCondition` is a Tier-3 leaf condition with one
field, `predicate ∈ {last_would_have_won, last_would_have_lost, last_fired}`.
It carries no `n` — every predicate is a `last_*` test (the run-length
`consecutive_*` predicates have no `prior_signal` analogue in this scope).
`prior_trade` is unchanged; the two coexist and route to the same iterative
engine.

**What "a signal" is.** The iterative simulator splits a `prior_signal` entry
into a *raw signal* and a *gate*. The supported shape is `and(core…, gate…)`:
the `core` children (none Tier-3) are the signal generator — the bars that
*are* signals — and the `gate` children (each Tier-3) decide fire vs skip. A
bar is an evaluated signal iff the raw signal is true *and the strategy is
flat* (a breakout while already in a position is not a new entry signal).
Entry shapes outside `and(core, gate)` raise `IterativeBacktestError` rather
than being silently mis-modelled — the same fail-loud policy the simulator
already applies to unsupported shapes.

**Phantom outcomes.** When the gate skips a signal, the simulator computes a
*phantom outcome*: it simulates the trade the entry would have produced —
same next-open fill, same stop / take-profit / time / condition exits, same
fee and slippage model as a real trade — and classifies the resulting return
into win / loss / breakeven. Phantom outcomes are size-independent (a trade's
return % does not depend on position size), so the phantom needs no sizing.
They are recorded in a parallel `SignalHistory` ledger and **never touch
equity, the trade list, win rate, or any real metric** — they exist only to
give `prior_signal` something to evaluate. A signal that *fired* is scored by
its real trade's outcome; the two paths fill the same `SignalRecord` shape.

**No look-ahead.** A signal's outcome is only *known* once its (real or
phantom) trade has closed. Every `SignalRecord` carries a `resolved_bar`;
`prior_signal` at bar *M* consults only the most recent signal with
`resolved_bar ≤ M`. The phantom is *computed* eagerly — it is pure arithmetic
over the frozen price arrays, deterministic, no clock, no randomness — but its
result is *visible* only from the bar the hypothetical trade would have closed.
A `prior_signal` evaluated before any signal has resolved is false, exactly
like `prior_trade` on an empty history.

**Why the latch breaks.** Every breakout — fired or skipped — now leaves a
`SignalRecord`. "The most recent signal" advances on every breakout, so the
gate tracks the live sequence of breakouts instead of freezing on the first
winner. Turtle System 1 re-run with `not(prior_signal last_would_have_won)`
produces a full multi-year ledger (acceptance target: >50 trades on the 6-year
BTC 4h dataset) instead of the single trade `prior_trade` produced.

**`prior_trade` vs `prior_signal`.** They are deliberately distinct:
`prior_trade` reasons about trades the strategy *took* ("after a losing
trade", "stop after 3 losses"); `prior_signal` reasons about every signal the
strategy *evaluated*, taken or not ("skip the breakout if the last breakout
would have won"). A rule that reasons about skipped signals needs
`prior_signal`; a rule that reasons only about realised trades needs
`prior_trade`. The extraction prompt teaches this boundary explicitly (§3.3).

**Scope.** `prior_signal` lives in entry conditions (the gate position);
`prior_signal` inside an exit condition is not supported by the A.3b iterative
exit compiler and raises. Phantom computation adds bounded overhead — one
short forward simulation per skipped signal — and the 6-year Turtle backtest
stays comfortably inside the performance budget.

---

## 5. Overfitting analysis changes

(Grounded in `walk_forward.py`, `parameter_sweep.py`, `monte_carlo.py`,
`composite.py`.)

**Update (A.4 design).** §5 is the A.4 sub-phase: making walk-forward,
parameter sweep, and Monte Carlo *measure stateful specs correctly*. The
analyses already *run* stateful specs without error — each calls
`run_backtest`, whose A.3b router sends Tier-3 specs to the iterative engine
and everything else to vectorbt — so A.4 is about measurement **correctness**,
not crashes. §5.1–§5.3 below were written design-time, before A.3a/A.3b
shipped the iterative engine; each now carries an "Update (A.4 design)" block
resolving it. The fourth analysis, deflated Sharpe, is tier-agnostic — it
operates on the headline metrics, not the bar series — and needs no A.4
change.

### 5.1 Parameter sweep

`parameter_sweep.py` detects sweepable axes (`_detect_axes:153`) and builds a
5-point neighbourhood per axis (`_neighborhood_*`). Phase A adds:
- A `SweepAxisKind.RATCHET_RESET` is *not* meaningful (it's an enum, not
  numeric). The sweepable stateful parameters are the **numeric** ones inside
  child expressions — and those are *already swept* (indicator periods, stop
  pcts). The genuinely new numeric knob is none for `regime_state`/`ratchet`
  themselves (they have no numeric parameter — `extremum`/`reset` are enums).
- **Therefore parameter-sweep needs little change for T2.** A regime built
  from, say, a Supertrend-like `compare` already exposes its indicator periods
  to the existing `INDICATOR_PERIOD` axis. This is a pleasant finding: the
  composable design (§1.4) means stateful conditions inherit sweepability from
  their child expressions for free.
- T3 `prior_trade.n` *would* need a new additive neighbourhood
  (`[n-2, n-1, n, n+1, n+2]` clamped ≥1) — deferred with T3.

**Update (A.4 design).** Confirmed against the code: `_detect_axes`
(`parameter_sweep.py:153`) is a *blind* recursive walk over the whole spec
dict — it recurses into every dict value and list item — so an `indicator`
node anywhere, including inside `regime_state.enter_when` / `exit_when` or a
`ratchet.source`, is already discovered and swept on the existing
`INDICATOR_PERIOD` axis. The "inherits sweepability for free" claim therefore
**holds for T2 with zero code change**. A swept v2 cell is re-validated
(`StrategySpec.model_validate`) before it runs, so a mutation that violates a
v2 rule (e.g. an indicator-period change making `regime_state.enter_when ==
exit_when`) is caught and the cell skipped via the existing
`parameter_sweep_cell_invalid` path — no new handling needed.

`prior_trade.n` is the one genuine stateful numeric knob (T3 has shipped, so
"deferred with T3" is moot). A.4 nonetheless **leaves it unswept by design**:
`n` is meaningful only to the `consecutive_*` predicates, such specs are rare,
and `prior_signal` — the primary T3 primitive (§4.7) — has no numeric
parameter at all. Adding a `PRIOR_TRADE_N` axis (a new `SweepAxisKind`, an
additive integer neighbourhood `[n-2 .. n+2]` clamped to `[1,100]`, int-cast
on mutation) is small and well-understood; it is recorded here as a
low-priority follow-up, not A.4 scope.

### 5.2 Walk-forward — fold boundaries are state discontinuities

`walk_forward.py` splits `[start,end]` into contiguous non-overlapping windows,
each independently `run_backtest`-ed (`_run_segment:145`). **No state crosses
a fold boundary** — every segment is a cold `run_backtest`.

For T2 this is a real correctness problem: a regime that latched 400 bars
before a fold's OOS start begins that fold *un-latched*, mismeasuring it.
Phase A fix: `_run_segment` for a v2 spec runs the numba scan over a
**warm-up prefix** (`[segment_start - warmup_bars, segment_start]`) and
discards the prefix bars from the result. `warmup_bars` for a stateful spec is
"unbounded in principle" — use a generous fixed prefix (e.g. the full
in-sample span, or a capped 2,000 bars) and document it as an approximation.
Fold *results* still do not cross boundaries; only the *state warm-up* does.

**Update (A.4 design).** The warm-up-prefix sketch above is superseded by a
simpler, *exactly correct* design that MarketMind's walk-forward makes
available: it does **no per-window re-optimisation** — the docstring is
explicit that the same fixed spec runs on every window. For a no-refit
walk-forward, running each segment with a full warm-up prefix is identical,
bar for bar, to running **one continuous backtest over `[full_start,
full_end]` and slicing its trades and equity curve into the windows** — and
the single run costs ~1× the data span, versus the ~7× a per-segment
refetch-with-prefix costs across six windows.

**A.4 decision — for stateful (v2) specs, walk-forward is one continuous run,
sliced:**

- `run_walk_forward` does a single `run_backtest(spec, full_start, full_end)`;
  regime latch, ratchet extremum, `TradeHistory` and `SignalHistory` evolve
  continuously across the whole span. This is not a compute trick — it is the
  *faithful* model of a stateful strategy, matching how the A.5 trader will
  maintain state across cycles.
- Each window's IS/OOS metrics come from slicing that run: trades with
  `entry_time` in the window's range, and equity points with timestamp in it,
  fed to `compute_metrics`. Window boundaries and the IS/OOS split are
  unchanged from v1, so `WindowResult` and `_aggregate` are untouched.
- This resolves the open questions directly. **Warm-up length** — full
  history; no cap, no per-spec heuristic. The continuous run *is* the maximal
  warm-up: a 2 000-bar cap would be wrong for a `ratchet reset="never"` whose
  extremum is older than that, and a slow regime needs an unbounded prefix in
  principle — the continuous run sidesteps both. **T2 vs T3 warm-up** — moot;
  one continuous run warms every tier (indicator series, regime flips,
  trade/signal history) at once, so no tier-specific detection is needed.
  **Prefix-entered trades** — a trade is attributed to the window containing
  its `entry_time`. **Equity normalisation** — each window's return is
  relative to the equity at *that window's start* on the continuous curve, not
  a fresh `initial_capital` base; v2 window returns are continuous-curve-
  relative (document this — it differs from v1's fresh-capital-per-segment).
  **Per-fold or once** — once: a single run, sliced into all folds.
- **v1 specs are unchanged.** Branch on `condition_uses_stateful_v2`: false →
  today's cold per-segment path, **bit-identical** numbers (the walk-forward
  regression gate holds); true → the continuous-run-sliced path. A failure of
  the single run degrades to all-zero windows, mirroring today's per-segment
  exception handling.

One acknowledged approximation: a position open at a window boundary has its
entry counted in one window and its exit P&L realised in the next window's
equity slice — a minor smear, and only for strategies holding across
~month-scale boundaries. Forcing positions flat at boundaries would
re-introduce the very state discontinuity §5.2 fixes; the smear is the honest
lesser evil, documented rather than "fixed".

### 5.3 Monte Carlo

`monte_carlo.py` shuffles per-bar log returns and rebuilds synthetic OHLCV
with `open=high=low=close` (intrabar-flat, `monte_carlo.py:213`). Two
consequences, both **documented, not fixed**:
- Stop-driven behaviour is already degenerate on MC synthetic bars (no
  intrabar range) — a stateful ratchet stop is no worse off than today's
  trailing stop. The MC test "is about the return distribution, not bar
  shape" — keep that framing.
- Shuffling destroys autocorrelation — which is exactly the structure a
  `regime_state` depends on. A regime strategy *should* collapse toward the
  null on permuted data. That is **correct** behaviour and the composite
  scorer's interpretation note must say so, or a genuinely robust regime
  strategy will look falsely fragile.

**Update (A.4 design).** The second bullet's instinct is right but its
mechanism needs sharpening. The permutation shuffles *log returns*, which
**preserves the cumulative drift** (`Σ log returns` is shuffle-invariant, and
`monte_carlo.py` re-anchors to the real first open). Every synthetic series
therefore ends at the same price as the real one, so a long-biased strategy
still captures that drift on synthetic data. The MC p-value is
`P(synthetic_return ≥ real_return)` on **raw return**. A defensive stateful
strategy — a regime that sits *out* of drawdowns, a skip-after-winner gate
that declines signals — deliberately trades raw return for risk-adjusted
return, and so often *underperforms* the drift-capturing synthetic runs on raw
return. That inflates its p-value and makes a genuinely sound strategy look
overfit. The bias is not unique to stateful specs, but it bites them hardest
because risk management is their whole point.

**A.4 decision — the fix is interpretive plus a composite re-weight, not a
Monte-Carlo engine rewrite:**

- `compute_overfitting_score` (`composite.py`) currently takes the four result
  objects and no spec, so it cannot tell a stateful spec from a static one.
  A.4 passes it the spec (or a precomputed `is_stateful` flag from
  `condition_uses_stateful_v2`).
- For a stateful spec, **down-weight Monte Carlo** from 0.25 toward ~0.10 and
  move the freed weight to walk-forward (0.35 → ~0.50). Rationale: the
  return-permutation MC is a biased, lower-quality signal for stateful specs,
  while the now state-aware §5.2 walk-forward is the most direct and
  trustworthy check. v1 weights are **unchanged** (0.35 / 0.25 / 0.25 / 0.15);
  the exact stateful weights are a calibration choice (`composite.py` already
  flags its tables as v1, to be tuned in Phase 5).
- Attach an **interpretation note** to `OverfittingScore.explanation` for
  stateful specs: the MC permutation test compares against drift-preserving
  reshuffles and can understate a defensive, path-dependent strategy, so it is
  weighted lower here — a low MC score is not, on its own, an overfitting
  verdict.
- The synthetic-bar stop degeneracy (first bullet) applies to the **iterative
  engine** too: A.3b's intrabar check sees `high = close × 1.001`,
  `low = close × 0.999`, so a 2-ATR stop or a per-trade ratchet stop almost
  always fills on the close gap, never intrabar — the same documented
  degeneracy as v1's vbt trailing stop, no worse.

Deferred (not A.4): a genuinely better MC test for defensive/stateful specs
would score on **risk-adjusted** terms — a p-value on `real_sharpe` against a
distribution of *synthetic* Sharpes (`monte_carlo.py` records `real_sharpe`
but not the synthetic ones), or a drift-detrended permutation. That is a
Monte-Carlo redesign with its own validation surface, out of A.4 scope.

---

## 6. Trader template changes

(Grounded in `trader/templates/base.py`, `signal_engine.py`, `runner.py`,
`jobs.py`, the trader migrations, and `schemas/trader.py`.)

### 6.1 The determinism invariant Phase A must consciously break

The trader runtime is *provably stateless across cycles*: `base.py:1-9` —
"same inputs ⇒ identical outputs, every call: no clock reads, no I/O, no
randomness." Every cycle is reconstructed from `(candle window) + (open
PaperPosition)`. All five v1 templates have an empty `__init__` beyond
`self.params`.

A stateful strategy **deliberately breaks this** — `evaluate()` becomes a
function of accumulated history. That is acceptable but must be done
consciously and in isolation, so the five v1 templates stay provably pure.

### 6.2 New persistence — `trader_strategy_state` (migration `0013`)

Per-version mutable state **cannot** be a column on `trader_strategy_versions`:
the immutability trigger (`0006_trader_v1_strategies.sql:81-111`) rejects
writes to every column except `enabled`/`approved_for_paper`/`notes`. State
must be a new table, auto-applied at runner boot by `apply_migrations`
(`runner.py:172`).

```sql
-- infra/db/migrations/0013_v2_trader_strategy_state.sql
CREATE TABLE IF NOT EXISTS trader_strategy_state (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id         UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    symbol                      TEXT NOT NULL,
    state                       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at_candle_close_ts  TIMESTAMPTZ,   -- idempotency guard, see 6.4
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy_version_id, symbol)
);
```

Grain is `(strategy_version_id, symbol)` — it matches how templates are
instanced and lets state survive **while the strategy is flat** (a
`PaperPosition` only exists while a position is open, so regime/skip state has
nowhere to live today). `state` is one JSONB column, typed on read/write
through the `RatchetState`/`RegimeState` models from §1.3.

### 6.3 Template interface

Add a `StatefulStrategyTemplate` subtype rather than changing the base
`evaluate(candles, position)` signature — this keeps the five v1 templates
**untouched and still provably pure**:

```python
class StatefulStrategyTemplate(StrategyTemplate):
    def evaluate_stateful(
        self, candles: pd.DataFrame, position: PaperPosition | None,
        state: StrategyState,
    ) -> tuple[SignalEvaluation, StrategyState]:
        ...
```

`signal_engine._evaluate_pair` (`signal_engine.py:521`) gains a
`_load_strategy_state` helper (mirroring `_open_position`), dispatches to
`evaluate_stateful` for stateful versions, and persists the returned next
state with `INSERT ... ON CONFLICT (strategy_version_id, symbol) DO UPDATE`.

### 6.4 Atomicity and idempotency — the subtle part

- **Atomic write.** The state UPDATE must commit inside the **same
  `with conn.transaction()`** block as the `trader_signals` write
  (`signal_engine.py:539`). CLAUDE.md's Phase 2 lesson — "a row that records
  'we already acted' must commit with the row that depends on it." If state
  advanced but the signal write rolled back, the strategy skips a bar.
- **Idempotency.** The runtime is built to safely re-evaluate the same closed
  candle after a crash (`trader_signals` has `ON CONFLICT DO NOTHING`). But a
  HOLD writes no signal row — so a stateful HOLD that advanced a counter would
  **re-advance on restart**. Fix: before mutating state, compare the candle
  being evaluated against `updated_at_candle_close_ts`; skip the mutation if
  it already covers this bar. **This is the single subtlest correctness point
  in Phase A** and gets a dedicated test (§7).

### 6.5 Restart recovery

Free, once the table exists: state is reloaded from the DB at the start of
each cycle, exactly as `peak_equity` is (`0009` header explicitly establishes
this pattern) and open positions are. Nothing in process memory.

### 6.6 Drift parity — a hard constraint

`drift.py` compares live paper metrics to the backtest's
`backtest_metrics`. Its validity rests on paper and backtest producing
*comparable trade streams*. **A stateful strategy whose live behaviour depends
on accumulated state will diverge from a stateless single-pass backtest unless
the backtest replays the identical state machine.** Therefore the §4 numba
scan kernels and the §6.3 live `evaluate_stateful` **must implement the same
recurrence** — ideally sharing code in `marketmind_shared`. If they drift, the
drift analyzer flags healthy stateful strategies as decaying. This is a
correctness requirement, not a nicety, and §7 includes a parity test.

---

## 6A. A.5 design pass — trader stateful execution

(Design pass 2026-05-21, after A.1–A.4 + the prior_signal extension shipped.
§6.1–§6.6 above are the Phase-0 sketch; this section **supersedes them where
they conflict** — notably the §6.2 schema grain and the §6.4 idempotency
mechanism. Grounded in a fresh read of `signal_engine.py`, `runner.py`,
`templates/`, `drift.py`, the trader migrations, and
`scripts/trader_seed_strategy.py`. The four open questions this pass raised
were **resolved by the reviewer on 2026-05-21** — §6A.9 records the answers,
and the sections below are written to those decisions.)

### 6A.0 The blocking finding — the trader has no spec executor

The §6.1–§6.6 sketch assumed the trader can already *run* a stateful strategy
and only needs state plumbing. It cannot. The trader runs **five hand-coded
templates** — `build_template` (`templates/__init__.py:52`) is an exhaustive
dispatch over the five `TemplateName` members (ma_trend, breakout,
rsi_mean_reversion, bb_mean_reversion, vcb). There is **no generic
`StrategySpec` executor**. `scripts/trader_seed_strategy.py` hard-wires those
five names; a v2 spec — Turtle (`prior_signal`), Supertrend (`regime_state`) —
**cannot be seeded into the trader today**.

So A.5 has a prerequisite the brief did not budget for: **a path from a v2
`StrategySpec` to a runnable trader strategy.** Two options were weighed:

- **(a) Hand-coded stateful templates** — a `SupertrendTemplate`, etc.,
  mirroring the five v1 templates. Minimal for the Phase-A acceptance (one
  stateful strategy live) but does not scale and is not the platform premise.
- **(b) A generic `SpecTemplate`** — a sixth template that carries a
  `StrategySpec` and evaluates its condition tree by **reusing the backtest
  evaluators**. This is the platform-correct answer ("submit a URL → extract
  → run") and the only one that structurally guarantees the §6.6 drift parity
  (one evaluator implementation, not two).

**Resolved (6A.9-Q1): option (b), the generic `SpecTemplate`.** Hand-coded
templates would create a second evaluator that has to be kept byte-identical
to the backtest's by hand — exactly the divergence §6.6 forbids. The
`StrategySpec` schema exists *so that there is one source of truth*; the
trader must run that, not a parallel transcription. For A.5's T1+T2 scope
(see 6A.9-Q4) the `SpecTemplate` reuses `translator._eval_condition` /
`_eval_regime_state` / `_eval_ratchet`; the `iterative` engine's per-bar
evaluators (the T3 path) are wired in later, by A.6.

This is **sub-phase A.5a** (§6A.8) — the gate on everything else. The state
mechanics below (§6A.1–§6A.5) are independent of A.5a's internals.

### 6A.1 State persistence — `trader_strategy_state` (migration `0013`)

**Grain: `(strategy_version_id, symbol, timeframe)`.** The §6.2 sketch omitted
`timeframe` — wrong: a version may run several timeframes (`version.timeframes`
is a list) and a 4h regime latch is not a 1h regime latch. State is per
timeframe.

**Shape: append-only, one row per evaluated candle, JSONB state blob.**

```sql
-- infra/db/migrations/0013_v2_trader_strategy_state.sql
CREATE TABLE IF NOT EXISTS trader_strategy_state (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id  UUID NOT NULL
        REFERENCES trader_strategy_versions (id) ON DELETE CASCADE,
    symbol               TEXT        NOT NULL,
    timeframe            TEXT        NOT NULL,
    candle_close_ts      TIMESTAMPTZ NOT NULL,   -- candle this state is "as of"
    state                JSONB       NOT NULL,   -- StrategyState, Pydantic-typed
    state_schema_version INTEGER     NOT NULL DEFAULT 1,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy_version_id, symbol, timeframe, candle_close_ts)
);
CREATE INDEX IF NOT EXISTS ix_trader_strategy_state_current
    ON trader_strategy_state (strategy_version_id, symbol, timeframe, candle_close_ts DESC);
```

- **Append-only**, as the brief prefers. A state advance `INSERT`s a new row;
  state is never `UPDATE`d. The full trajectory is its own audit log, and
  rollback is "ignore rows after timestamp T". No `is_current` flag and **no
  separate pointer table**: maintaining a flag would require an `UPDATE` per
  advance (defeating append-only), whereas `ORDER BY candle_close_ts DESC
  LIMIT 1` over the DESC index *is* the current-state pointer, index-only and
  free. (`trader_strategy_versions`' immutability trigger from `0006` does not
  apply — it guards that table, not this one; append is the intended write.)
- **JSONB blob, not structured columns.** The state is heterogeneous and
  strategy-dependent — ratchet extrema, regime latch booleans, and (later) a
  `SignalHistory` list and `TradeHistory` — with no fixed column set. One
  JSONB column, read/written through a Pydantic `StrategyState` model (new, in
  `marketmind_shared.schemas.trader`, alongside the §1.3 `RatchetState` /
  `RegimeState` — none of which exist yet).
- **A.5 scope of the blob (6A.9-Q4): T1 + T2 only.** A.5's `StrategyState`
  carries the Tier-2 state — `RegimeState` (latch flag + last-flip timestamp)
  and `RatchetState` (running extremum) — and nothing else. Tier-1 is
  bounded-window and re-derived from the candle window each cycle, so it
  needs no row at all. The Tier-3 state — `SignalHistory` (with phantom
  outcomes) and `TradeHistory` — is **not persisted in A.5**; live `prior_signal`
  / `prior_trade` defer to A.6. The `trader_strategy_state` *table* is generic
  (a JSONB blob), so A.6 adds T3 fields to `StrategyState` with no migration —
  only `state_schema_version` ticks.
- **`UNIQUE (version, symbol, timeframe, candle_close_ts)`** mirrors
  `trader_signals` exactly (`0008`) — the same idempotency-by-construction
  pattern (§6A.2), and `INSERT ... ON CONFLICT DO NOTHING` is the cross-worker
  race safety-net.
- **State versioning (v3 readiness).** `state_schema_version` records which
  `StrategyState` shape wrote the row. The `StrategyState` model gives every
  field a default, so a v3 reader of a v2 row gets defaults for new fields;
  the next advance re-writes the row in v3 shape. Append-only makes this
  painless — old rows are immutable history, only the live tip migrates
  forward.

### 6A.2 The idempotency guard — the named correctness point

**The re-evaluation that creates the hazard.** `tick_main_cycle` runs **every
minute** (`runner._bootstrap_scheduled_jobs` → `next_minute_boundary`). A
strategy on a 4h timeframe therefore has its latest closed candle *C*
re-evaluated by ~240 consecutive ticks before *C+1* closes. For a **fired**
signal that is harmless — `_signal_exists` + the `trader_signals` UNIQUE
dedupe it (`signal_engine.py:550`). But a **HOLD writes no signal row**, so
`_signal_exists` stays false and a HOLD candle is genuinely re-evaluated every
minute. A stateless template re-HOLDing is idempotent; a stateful evaluation
that *advances and persists state* on every HOLD would advance it ~240× per
candle. That is the bug CLAUDE.md §6.4 names.

**The contract.** The stateful step is one pure function

```
step(prev_state, candle_window, position) -> (next_state, decision)
```

idempotent on `prev_state`. The guard's job: **call `step` exactly once per
candle, always from the true `prev_state` (the state as of `C-1`).**

**The mechanism — a timestamp comparison, no new column needed.** Inside
`_evaluate_pair`'s existing single transaction, for a stateful version:

1. Read the current state row: `… ORDER BY candle_close_ts DESC LIMIT 1`.
   Its `candle_close_ts` is `prev_ts`; absent ⇒ cold start.
2. **`latest_close_ts > prev_ts`** (or cold) — *fresh candle.* Call `step`;
   `INSERT` the new `(…, latest_close_ts, next_state)` row; if `decision` is a
   signal, `_persist_signal` — **both writes in the one transaction.**
3. **`latest_close_ts == prev_ts`** — *already stepped.* Do **not** call
   `step`, do **not** `INSERT`. Reaching here means *C* did not fire a
   persisted signal (else `_signal_exists` would have skipped the pair
   upstream), so *C*'s decision was HOLD — re-emit HOLD. The state row is
   untouched.
4. **`latest_close_ts < prev_ts`** — candle went backwards (clock/data
   anomaly). Treat as already-stepped; log a warning.

The `ON CONFLICT DO NOTHING` on the `INSERT` is the belt to step 3's braces:
even if a second worker raced past the advisory lock, the duplicate
`(version, symbol, timeframe, candle_close_ts)` row is silently dropped.

**Atomicity.** The state `INSERT` and the `trader_signals` `INSERT` commit in
the **same `with conn.transaction()`** block `_evaluate_pair` already opens —
CLAUDE.md's Phase-2 lesson ("a row recording 'we already acted' commits with
the row that depends on it"). A crash mid-transaction rolls back both; the
re-run sees `prev_ts = C-1` and correctly re-does *C* from scratch.

**Failure-mode analysis (both directions, as the brief asks).**
- *Over-firing* (guard too weak — state re-advances): a counter-style
  condition ("N bars since exit") drifts, the strategy mis-fires entries with
  real (paper) money downstream. **Unacceptable.**
- *Over-conservatism* (guard too strong — a genuine new candle is treated as
  already-stepped): the strategy misses a bar's signal. A missed entry is an
  opportunity cost, not a wrong action; the next candle re-evaluates and the
  strategy recovers. **Bad, but strictly safer than over-firing.**
- The guard is therefore deliberately biased to the conservative side: the
  comparison is `>` (strict) for "fresh", so an exact-tie defaults to
  already-stepped. The only way to *miss* a candle is a `candle_close_ts`
  collision, which the upstream candle ingestion already forbids.

This is the §6A.7 dedicated test.

### 6A.3 Recovery sequence

**Restart recovery is the steady-state cycle path — there is no separate
recovery code.** On container restart the runner re-boots (`runner.main`),
re-applies migrations, and resumes ticking. The next `_evaluate_pair` reads
the most-recent `trader_strategy_state` row — and *that row is the recovered
state*. `SignalHistory`, ratchet extrema, regime latches, `TradeHistory` all
live in that one JSONB blob. This is exactly the `peak_equity`-from-
`trader_portfolio_snapshots` pattern the trader already uses (`0009`); §6.5's
"free, once the table exists" holds, and is now literally the same code path
as a normal cycle.

A crash *mid-cycle* rolls its transaction back (§6A.2), so the most-recent row
is `C-1`; recovery re-evaluates *C* fresh. Nothing in process memory is
load-bearing.

**Unreconstructable state — the safe fallback.** If the most-recent row is
missing or fails to deserialize into `StrategyState` (corruption, a botched
migration): the version is **disabled** (`enabled = FALSE`) and a
WARNING-severity `trader_alerts` row is written. A stateful strategy must not
trade on *unknown* state — a cold-started regime latch (un-latched) or an
empty `SignalHistory` (a `not(prior_signal …)` gate reads wide-open) would
fire entries the strategy's real state would have blocked. **Disable-and-alert
is the decided behaviour (6A.9-Q3): never trade on unknown or corrupt state.**
A recoverable failure mode — the strategy stops, the operator is alerted, the
state is rebuilt or the version re-seeded — is always better than a wrong
action with money. "Cold-start and keep trading" is rejected.

### 6A.4 Drift-analyzer integration

`drift.py` compares **metric-level** live-vs-backtest (trade count, win rate,
avg return, weekly frequency) in 30% / 60% health bands — it does not compare
signals bar-by-bar. For a stateful strategy that comparison stays valid **iff
the live state machine and the backtest state machine are the same code** —
the §6.6 parity constraint. A.5 satisfies it structurally by routing the live
stateful evaluation through the **same** backtest evaluators (the §6A.0(b)
`SpecTemplate` reuses `translator` / `iterative`; a hand-coded template must
import them, never re-implement the recurrence). With shared code on identical
candle data the live and backtest state trajectories are identical, so the
existing metric drift analyzer needs **no change** for A.5.

A finer **state-trajectory drift** signal — "is the live regime latching on
the same bars as the backtest; is `SignalHistory` diverging cumulatively" — is
a genuine new dimension but **not needed for A.5** (shared code makes it
redundant) and is **deferred to A.6/A.7**. Flagged here as the brief requested.

### 6A.5 Migration `0013` deployment & rollback

- **When it runs.** `apply_migrations` at runner boot (`runner.py:173`),
  idempotent, `CREATE TABLE IF NOT EXISTS`. Activating A.5 needs a
  `trader_worker` container rebuild (the first time this Phase-A constraint is
  deliberately lifted).
- **Backward compatibility.** `0013` only *adds* a table; no existing table or
  row changes. v1 (stateless) strategies never touch `trader_strategy_state` —
  the cycle reads/writes it only on the stateful branch — so they have **zero
  rows** and run byte-identically. No empty rows, no default state for v1.
- **Rollback.** If A.5 ships a bug, revert the trader code to pre-A.5: that
  code has no knowledge of `trader_strategy_state` and simply ignores the
  table — an unused table is inert. In-flight state rows are orphaned but
  harmless. A later A.5 re-deploy resumes from the last row (the append-only
  trajectory shows any gap). The migration itself is not un-applied (an
  additive table is safe to leave); `_schema_migrations` keeps its record.

### 6A.6 Risk areas

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | **Idempotency guard** — state re-advances on a re-evaluated HOLD candle (§6A.2) | High | Strict `>` timestamp guard + `ON CONFLICT` net + same-transaction atomicity; dedicated regression test (§6A.7) |
| 2 | **Live⇄backtest state divergence** — two implementations of one recurrence drift; the metric drift analyzer would not catch it for days | High | One evaluator implementation — the live path reuses `translator` / `iterative`, never re-implements (§6A.4, §6.6) |
| 3 | **No spec executor** — a v2 spec cannot run in the trader at all (§6A.0) | High | A.5a builds the generic `SpecTemplate` reusing the backtest evaluators (6A.9-Q1, resolved) |
| 4 | **`0013` in production** | Low | Additive `CREATE TABLE IF NOT EXISTS`; safe with existing rows; rollback-inert (§6A.5) |
| 5 | **Corrupt/unreadable state row** | Medium | Disable version + alert; never trade on unknown state (§6A.3) |

### 6A.7 Acceptance criteria for A.5 implementation

1. `test_trader_idempotency_regression.py` — re-running a cycle on the same
   closed candle advances `trader_strategy_state` **exactly once** (the
   persisted state is `state_C`, never double-advanced); a HOLD candle
   re-evaluated N times leaves one state row.
2. `test_trader_state_recovery.py` — a simulated restart (drop in-process
   state, re-read from the table mid-strategy) reproduces the identical
   downstream decision.
3. **v1 unchanged** — BB Breakout and Golden Cross seed, evaluate, and signal
   exactly as before; they write zero `trader_strategy_state` rows.
4. **Container rebuild safe** — `0013` applies cleanly on a DB with existing
   v1 strategies; the stateless and stateful versions coexist.
5. A T2 stateful strategy (Supertrend) seeds, is approved, evaluates live, and
   its regime latch persists + reloads across a restart.
6. The full suite (917+ at A.5 start) stays green; `uv run pyright` clean.

### 6A.8 Sub-phase breakdown — finalized

A.5 is **three sub-phases, ~3–4 sessions** — not one. It mirrors the
A.3a→A.3b split in the backtest engine — T1+T2 first, T3 after (6A.9-Q4).
Plan and review A.5a → A.5b → A.5c as separate sessions; A.5a gates the rest.

- **A.5a — the generic `SpecTemplate`.** A sixth trader template carrying a
  `StrategySpec`, evaluating its entry / exit / filter condition tree by
  reusing the backtest's `translator` evaluators (`_eval_condition`,
  `_eval_regime_state`, `_eval_ratchet`) — one evaluator, one source of truth
  (6A.9-Q1). Scope: **T1 + T2 conditions** (6A.9-Q4); the T3 path (the
  `iterative` engine, `prior_trade` / `prior_signal`) is *not* wired into the
  trader in A.5. New `TemplateName.SPEC` member + the `0006` `template`
  CHECK-constraint extension (a small migration) + the
  `test_trader_enum_db_parity` mapping; `scripts/trader_seed_strategy.py`
  extended to seed a v2 spec as a `SpecTemplate` version. Acceptance:
  Supertrend (pure T2 regime) seeds, is approved, and produces live signals.
  The largest, least-mechanical piece. **1–2 sessions.**
- **A.5b — state persistence + the idempotency guard.** Migration `0013`
  (`trader_strategy_state`, §6A.1); the `StrategyState` Pydantic model — for
  A.5 it carries **T2 state only**, `RegimeState` + `RatchetState`; the
  `load → step → strict-> guard → atomic INSERT` logic in `_evaluate_pair`
  (§6A.2); `test_trader_idempotency_regression.py`. This is the CLAUDE.md
  §6.4 "subtlest correctness point". **1 session.**
- **A.5c — recovery + drift parity.** `test_trader_state_recovery.py`; the
  disable-and-alert corrupt-state fallback (§6A.3); the shared-evaluator
  drift-parity test (§6A.4). Folds partly into A.5b. **~0.5–1 session.**

T3 live execution — `prior_trade` / `prior_signal` evaluated from the
deterministic signal layer (6A.9-Q2), with `SignalHistory` + live phantom
outcomes persisted into the same `trader_strategy_state` blob — is **A.6**,
not A.5.

### 6A.9 Resolved decisions (reviewer, 2026-05-21)

The four open questions this design pass raised have been resolved by the
reviewer; the sections above are written to these answers.

1. **Spec-execution path → the generic `SpecTemplate`.** Build the spec
   executor that runs any extracted spec through the shared backtest
   evaluators. Hand-coded templates would create two evaluators that have to
   be kept in sync by hand; the `StrategySpec` schema exists precisely so
   there is one source of truth. (Drives §6A.0, A.5a.)
2. **`prior_trade` / `prior_signal` → simulation-consistent.** They evaluate
   from the deterministic signal layer — the same logic the backtest runs.
   The risk manager gates order *execution* downstream; it does **not** feed
   back into signal logic. (Applies to A.6, where T3 lands live.)
3. **Corrupt / unreconstructable state → disable-and-alert.** The strategy
   disables itself and alerts; it never trades on unknown or corrupt state.
   A recoverable failure mode is always better than a wrong action with
   money. (Drives §6A.3.)
4. **A.5 scope → T1 + T2 only.** A.5 ships T1 and T2 live; Supertrend (pure
   T2) is the acceptance test. T3 — `prior_signal` / `prior_trade` plus
   `SignalHistory` persistence and phantom outcomes computed live — defers to
   **A.6**, mirroring the A.3a→A.3b split in the backtest engine. (Drives
   §6A.1's blob scope and §6A.8.)

---

## 6B. A.5b design — the state-seeding mechanism

(Design pass 2026-05-21, opening the A.5b implementation session. A.5a
shipped the stateless `SpecTemplate`; A.5b persists T1+T2 state and adds the
idempotency guard. A.5a surfaced one open mechanism question — *how the
persisted state feeds the evaluation* — resolved here before implementation.)

### 6B.0 Migration-number reconcile

§6A wrote the `trader_strategy_state` migration as `0012`. **A.5a took `0012`**
for the `template` CHECK-constraint widening, so `trader_strategy_state` is
**migration `0013`** — §6A's references above are updated accordingly.
`0012` = the A.5a CHECK widening; `0013` = the A.5b state table.

### 6B.1 The mechanism question

A.5a's `SpecTemplate` evaluates a spec through `translator.build_signals` —
vectorised, whole-window, stateless. For a T2 spec the regime latch / ratchet
extremum it computes reflects only the loaded candle window: if the last
regime flip predates the window, the latch is wrong. A.5b must make the live
latch full-history-exact. Three candidates:

- **A — seed the shared evaluator.** Thread the persisted state into
  `_eval_regime_state` / `_eval_ratchet` so each recurrence starts from a
  checkpoint instead of from `initial`.
- **B — iterative engine, live only.** Route the live T2 path through the
  `iterative` engine; keep the backtest on `build_signals`.
- **C — iterative engine, everywhere.** Route every stateful spec through the
  iterative engine in both backtest and live.

### 6B.2 Decision — Mechanism A

- **B is rejected.** It gives T2 two backtest-vs-live evaluation *paths* with
  different fill models — exactly the divergence §6.6 forbids; `drift.py`
  would flag healthy strategies.
- **C is rejected.** Routing T2 backtests through the iterative engine
  changes their fill model (the iterative loop is not bit-identical to
  vectorbt), so every T2 backtest result shifts; it adds an unmeasured
  performance cost to the overfitting pipeline (parameter sweep × cells,
  Monte Carlo × permutations all on the Python-loop engine); and it
  re-architects backtest routing — far outside A.5b's scope.
- **A is chosen.** It is contained (a change inside `translator.py`), the
  backtest is **bit-identical** (the seed defaults to `initial`, a no-op),
  and there is no performance change. Crucially, there is *already one* T2
  evaluator: `iterative.py` imports and reuses `translator._eval_condition` /
  `_eval_regime_state`. A seeds that single evaluator; B and C route around
  it and re-introduce a second path. A needs **no user input** — it has no
  unmeasured performance implication.

### 6B.3 Seeding semantics — the seed is just the most-recent state row

The seed for `_eval_regime_state` is the regime latch as of the **last
evaluated candle** — the most-recent `trader_strategy_state` row. This is
correct because the latch is *"the value set by the most recent trigger"*,
not an accumulation:

- If an `enter_when` / `exit_when` trigger fires within the loaded window,
  the existing `marker.ffill()` carries it to the last bar — the seed is
  irrelevant.
- If no trigger fires in the window, the latch has been constant across it,
  so the seeded `fillna(seed_latch)` is exactly right — and `latch` as of the
  last evaluated candle *is* that constant value.

The ratchet is a running `max` / `min` — idempotent — so seeding
`_eval_ratchet` with the persisted extremum and folding the window's running
extremum into it (`max(seed, cummax(window))`) is correct. **Consequence: the
loaded window needs only the indicator-warmup margin — there is no unbounded
window.** Cold start (no prior row) seeds from `initial`; the latch is
window-truncated until the first in-window trigger and **exact thereafter** —
strictly better than A.5a's perpetual re-derivation.

### 6B.4 The HOLD case — one state row per evaluated candle

§6A.1 already specifies "one row per evaluated candle", and §6A.2's strict-`>`
guard needs `prev_ts` to track the last *evaluated* candle for its `==`
("already stepped") check to be crisp. Therefore: **a HOLD writes no signal
row (unchanged v1 behaviour) but does write a state row.** The state value may
equal the prior row's — that is fine; the row records "state as of candle C",
keeps the seed exactly one candle behind, and keeps the guard crisp. Skipping
rows for unchanged state would re-introduce an unbounded gap between the seed
and the new candle (§6B.3) — rejected.

### 6B.5 Implementation shape

`build_signals(spec, data) -> SignalSet` keeps its signature — every existing
caller is untouched. A seeded sibling, `build_signals_stateful(spec, data,
prior_state) -> (SignalSet, StrategyState)`, exposes the seeded evaluation and
returns the advanced state; both share one internal body, and `build_signals`
is that body with the `initial` seed (regime → `cond.initial`; ratchet →
∓∞, a no-op for the running extremum), which is bit-identical to today.
`StrategyState` is the JSONB payload — positional lists of `RegimeState` /
`RatchetState`, keyed by deterministic condition-tree walk order; a trader
version's spec is immutable, so positional keys are stable for its lifetime.

### 6B.6 What shipped (A.5b implementation, 2026-05-21)

A.5b landed across four commits. Recording the gaps between §6B-as-designed
and the code, per the §4.6-patch discipline:

- **Mechanism A shipped intact.** `build_signals_stateful` seeds the shared
  `_eval_regime_state` / `_eval_ratchet` and returns the advanced
  `StrategyState`; `build_signals` delegates to the same body with no seed
  and is bit-identical to before (verified — entries and exits equal). The
  `_evaluate_pair` idempotency guard, the atomic state+signal write, and one
  state row per evaluated candle (§6B.4) shipped as designed.

- **§6B.3's "exact" needs a caveat — the recursive-indicator window.** The
  seed makes the regime *latch state* full-history-exact. But the indicators
  *inside* a regime's enter/exit triggers — EMA, RSI, ATR — are recursive:
  their value at the window's last bar still carries exponentially-decaying
  memory of pre-window bars. With a bare warmup-margin window the seeded live
  evaluation diverged from the one-shot backtest at ~0.4% of bars (a marginal
  `close`-vs-`EMA200` crossover landing one bar off). Resolution:
  `SpecTemplate.min_bars_needed` loads **5× the indicator warmup** —
  `(1-2/p)^(4p) ≈ e⁻⁸` drives the truncation below ~0.1%, and the seeded
  walk then matches the one-shot bit-for-bit. So: the *seed* makes the latch
  exact; the *window size* makes the recursive indicators exact-enough.
  §6B.3's "the window needs only the warmup margin" is corrected to "~5× the
  warmup".

- **Corrupt-state handling is deferred to A.5c, as designed.** A.5b's
  `_load_strategy_state` treats an unparseable `trader_strategy_state` row as
  a cold start (and logs `strategy_state_unparseable`); §6A.3's
  disable-and-alert hardening is A.5c. A.5b never runs in production (the
  container is not rebuilt until after A.5c), so the lenient interim is never
  live.

- **Homes.** Migration `0013_v2_trader_strategy_state.sql`; `StrategyState` /
  `RegimeState` / `RatchetState` in `marketmind_shared.schemas.trader` (per
  §6A.1 — the backtest translator imports them, a valid workers→shared
  dependency); `build_signals_stateful` in `translator.py`;
  `SpecTemplate.evaluate_stateful` + the `is_stateful` property; the guard in
  `signal_engine._evaluate_pair`, with a new `pair_state_guarded` cycle stat.
  `RatchetState.reset_epoch` is built but unused — reserved for A.6's
  per-trade ratchets.

### 6B.7 What shipped (A.5c implementation, 2026-05-21)

A.5c hardened the corrupt-state path and added the drift-parity and recovery
gates — the last code-only sub-phase of Phase A's stateful trader. Four
commits; recorded per the §4.6-patch discipline:

- **Corrupt-state hardening — §6A.3 disable-and-alert shipped.**
  `_load_strategy_state` raises `_CorruptStateError` for an unparseable
  `state` JSONB or a `state_schema_version` this engine does not understand;
  `_evaluate_pair` catches it and `_handle_corrupt_state` sets the version
  `enabled = FALSE` and writes a WARNING `trader_alerts` row. No
  auto-recovery — the version stays disabled until an operator re-enables
  it. A.5b's lenient cold-start-on-unparseable fallback is gone.

- **Exception hardening — added.** `_evaluate_pair` wraps `evaluate_stateful`
  in a broad `except`: any failure evaluating a stateful spec routes to the
  same disable-and-alert path, so one version's evaluation bug cannot crash
  the cycle for the others. `_persist_strategy_state` is deliberately left
  outside the `except` — a real DB error there must surface, not be masked
  as corrupt state.

- **Drift parity — a test gate, not runtime monitoring.** §6.6's
  drift-parity requirement is satisfied structurally (one evaluator, §6B)
  and now *gated*: `test_drift_parity_supertrend_live_path_matches_backtest`
  walks the live `evaluate_stateful` path — round-tripping `StrategyState`
  through JSON each bar, as it passes through the JSONB column — and asserts
  zero divergence from the one-shot backtest. **Deferred, correctly, to the
  drift analyzer:** *runtime* drift detection — comparing live paper metrics
  to the backtest over days/weeks — is `drift.py`'s existing job (§6A.4);
  A.5c adds the static parity gate, not a new runtime monitor.

- **Recovery — free, and now gated.** §6A.3's "restart recovery is the
  steady-state cycle path" held with no new code: state lives only in
  `trader_strategy_state`, `evaluate_one_cycle` holds nothing across calls,
  so a restart is just the next cycle reading the DB.
  `test_state_survives_restart_and_resumes_at_the_next_candle` confirms it —
  and that the A.5b idempotency guard makes the restart's
  re-run-the-same-candle cycle a no-op.

- **New cycle stat.** `SignalEngineResult.pair_state_disabled` counts
  corrupt-state disablings — operator-visible in the cycle log.

**Phase A's stateful trader is now code-complete (A.5a + A.5b + A.5c).** The
operational step that follows is a one-time `trader_worker` container rebuild
to pick up the v2 code — that is *not* part of A.5c; it happens after an
end-to-end Phase-A review.

---

## 6C. A.6 design — live Tier-3 execution

(Design pass 2026-05-21, opening A.6 — **Phase 1 (design) only.** Per the
A.6 brief, implementation pauses after this section for reviewer sign-off:
A.6 is materially larger than the A.5b pattern, and the core decision
(§6C.2) carries real regression risk. Grounded in a fresh read of
`iterative.py`, `trade_history.py`, and the `0013` schema.)

### 6C.0 The problem — the iterative engine is not live-able as-is

A.6 runs a `prior_signal` / `prior_trade` (Tier-3) spec — Turtle System 1 —
in the live trader. The only Tier-3 evaluator is `iterative.py`'s
`run_iterative_backtest`: a **monolithic full-history forward-walk**. Two
properties make it un-live-able unchanged:

- It walks `range(n)` over the whole dataset in a single call — there is no
  per-bar resumable step.
- A skipped signal's **phantom outcome** is computed by `_phantom(bar)`,
  which walks *forward* (`range(fill_bar, n)`) to find the would-be trade's
  exit. The live trader has no forward bars — the phantom cannot resolve at
  the skip cycle.

A.5b's pattern (seed a vectorised function, advance one bar) does **not**
transfer: the iterative engine is not a function to seed, and `SignalHistory`
is an append-list — re-walking a window double-counts it (unlike the
idempotent, last-trigger-wins T2 latch). A.6 needs a genuinely **incremental
Tier-3 evaluator**. This is the materially-larger scope flagged for sign-off.

### 6C.1 SignalHistory / TradeHistory storage — Mechanism A (JSONB)

The brief weighs **A** (extend `trader_strategy_state.state` JSONB with the
histories) vs **B** (a new `trader_signal_history` table). **A is chosen.**
`SignalHistory` / `TradeHistory` are small lists that grow **per signal** — a
breakout — *not* per cycle; the brief's "every cycle adds a row" premise for A
is inaccurate (Turtle prints a handful of signals a month, on raw-signal bars
only). `prior_signal` / `prior_trade` only ever consult *the most recent
resolved* record — no indexing, no relational query. So a JSONB list inside
the existing `state` blob — typed by an extended `StrategyState`, with
`state_schema_version` 1 → 2 — is right: no new migration, atomic with the T2
state, one load per cycle (consistent with A.5b §6B). B buys indexing nothing
needs. A retention cap (keep the last *K* records) bounds the blob.

### 6C.2 The live Tier-3 evaluator — the decision for the reviewer

- **B2 — re-run `run_iterative_backtest` over a candle window each cycle.**
  *Rejected.* The window truncates `SignalHistory` (an old breakout falls
  out of view), and phantoms near the window edge hit `_phantom`'s
  "still-open → valued at last close" branch — degenerate, mark-to-market
  outcomes. As the window slides those flip to real outcomes, so
  `prior_signal` flickers: drift from the backtest.
- **B1 — refactor `run_iterative_backtest` into an incremental stepper**
  used by both the backtest (a loop over `step`) and the live trader (one
  `step` per cycle). The §6.6 one-evaluator answer, and the
  incremental-phantom model (§6C.3) then has a single implementation. But
  it refactors a load-bearing, numerics-sensitive module whose backtest
  output is gated bit-for-bit by `test_iterative.py` / `test_backtest_control.py`.
- **B3 — a separate incremental live-Tier-3 evaluator** that reuses every
  per-bar primitive (`_intrabar_exit`, `_stop_level`, `_update_trailing`,
  `_eval_condition`, the entry/exit evaluators) but has its own loop
  skeleton. The backtest engine is untouched (zero regression risk); the
  cost is a second ~30-line loop to keep in sync — a small drift surface,
  gated by a Turtle live-vs-backtest parity test.

**Recommendation: B1**, with B3 as the lower-risk fallback. B1 is the genuine
one-evaluator outcome and avoids two phantom implementations. But the
refactor touches the backtest's Tier-3 numerics; **the reviewer should sign
off on that risk, or choose B3.** This is the §6C stop-and-report decision.

### 6C.3 Phantom outcomes, live — pending phantoms

A skipped signal becomes a **pending phantom**: a mini-position carrying
`entry_fill` / `stop` / `tp` / `trail_anchor`. Each cycle advances every
pending phantom one bar through the existing `_intrabar_exit` /
`_update_trailing` / condition-exit primitives; when its exit fires the
phantom **resolves** — `(return_pct, resolved_bar)` — and `SignalHistory`
records it. `prior_signal` consults resolved-only: the existing
`_most_recent_resolved(current_bar)` gate is unchanged, so a live phantom
resolved at bar *R* is consultable from *R* onward — exactly as the
backtest's forward-peek `_phantom` gates it. **Drift-free by construction**:
the live phantom resolves at the same bar the backtest computes as
`resolved_bar`. Restart-mid-pending is free — pending phantoms live in the
persisted JSONB. `SignalHistory` needs two new methods: record a
skipped-but-pending phantom, and resolve a *specific* pending phantom by
`signal_bar` (several can be pending at once — unlike the single pending
*fired* signal `resolve_last_pending` assumes).

### 6C.4 prior_trade, live

`TradeHistory` is, per §6A.9-Q2, **simulation-consistent**: the incremental
stepper maintains a shadow-simulation `TradeHistory` — the signal layer's
trades — *not* `trader_paper_positions` (the risk-manager-filtered real
execution). The stepper produces both `SignalHistory` and `TradeHistory`, so
B1/B3 cover `prior_trade` and `prior_signal` together — no separate work.

### 6C.5 Scope, and the stop-and-report decision

A.6 is materially larger than A.5b: an incremental Tier-3 evaluator (a
refactor of, or a sibling to, the iterative engine), pending-phantom
mini-simulations, an extended `StrategyState`, the live `prior_signal` /
`prior_trade` evaluators, a shadow-simulation `TradeHistory`. Per the brief,
**implementation pauses here.** Reviewer decisions:

- **Q1 — B1 vs B3 (§6C.2):** accept the iterative-engine refactor for one
  evaluator (recommended), or take the sibling evaluator with a gated drift
  surface and zero backtest-regression risk.
- **Q2:** confirm the pending-phantom model (§6C.3).
- **Q3:** confirm Mechanism A storage (§6C.1).

With those signed off, the implementation is commits 2–6 of the A.6 brief.

### 6C.6 What shipped (A.6 implementation)

A.6 implemented §6C as designed, with the three sign-offs resolved: **B3**
(sibling evaluator), **Mechanism A** (JSONB storage), and the
pending-phantom model. Six commits on `v2-phase-a-stateful-conditions`.

- **Tier3State persistence (Mechanism A).** `StrategyState` gained an
  optional `tier3` block — `Tier3State` carries the shadow simulation's
  `signal_history` / `trade_history` / `shadow_position` / pending bars /
  `pending_phantoms` / `trade_id` / `cash` / `last_bar`. No new table, no
  migration 0014. `trader_strategy_state.state_schema_version` is stamped
  **2** for a row carrying a `tier3` block, **1** otherwise; the engine
  reads either (§6C-Q3).

- **`iterative_live.py` — the B3 sibling stepper.** `run_live_cycle`
  resumes the shadow simulation from a `Tier3State` checkpoint and
  advances it. It imports and reuses every per-bar primitive from
  `iterative.py`, which is left **untouched**. `_step` transcribes
  `run_iterative_backtest`'s STEP 1/2/4/5; `_advance_phantom` is
  `_phantom`'s loop body, stepped one bar per cycle.

- **The `bar < n-1` finding (§6C-incomplete at design time, now
  captured).** The backtest loop guards signal/time exits with
  `bar < n-1` — its last bar is end-of-data, an exit there cannot fill.
  The live stepper has no such bar: the latest bar is the *moving edge*,
  and a signal/exit there is recorded `pending` and fills next cycle. So
  the live `_step` carries **no `bar < n-1` guard**. The drift-parity test
  consequently compares the *settled* region — the backtest's
  end-of-data trade is excluded from the comparison.

- **The drift-parity gate — zero divergence.** `test_iterative_live_drift_
  parity.py` walks Turtle System 1 (1300 bars) through the iterative
  engine and the live stepper and asserts zero divergence: incremental
  (bar-by-bar, `Tier3State` JSON-round-tripped each cycle) equals the
  one-shot run on the full `Tier3State`; and the live stepper's settled
  trades equal `run_iterative_backtest`'s trades bit-for-bit. The gate
  caught a real bug — a reloaded position must keep its exact `size`,
  because `(s·a − s·b)/(s·b)` is not the float-exact equal of
  `(a − b)/b`; `Tier3ShadowPosition.size` is now persisted and the gate
  passes exactly.

- **Signal-engine wiring.** `SpecTemplate` accepts Tier-3 specs
  (`spec_template_rejection_reason` no longer rejects them);
  `evaluate_stateful` routes a Tier-3 spec through `run_live_cycle`, a
  Tier-2 spec through `build_signals_stateful` as before. The signal
  engine loads the full candle history for a Tier-3 version
  (`_TIER3_FETCH_BARS`) — the shadow `SignalHistory` is keyed on absolute
  bar indices, which must be stable across cycles. The A.5b idempotency
  guard and the A.5c disable-and-alert guard carry over unchanged.

A.6 is complete: the live trader runs Tier-3 (`prior_signal` /
`prior_trade`) specs. **Phase A's stateful trader is code-complete
(A.5 + A.6).** A.7 (the final regression sign-off) and the one-time
`trader_worker` container rebuild remain.

---

## 7. Test strategy

The full suite today: **763 `test_` functions** (`workers/tests` 555,
`api/tests` 116, `shared/tests` 63, root `tests/` 29) — the "~800" figure
counting parametrized expansions. Phase A must not regress any of them.

### 7.1 Unit tests (new)

- **Schema:** valid/invalid fixtures per new element (§2.3); round-trip and
  bounds suites pick them up automatically.
- **Validator:** one test per new `error_code` in §2.1.
- **Numba kernels:** `_regime_scan` / `_ratchet_scan` tested against a plain
  Python reference implementation over randomised inputs — the kernels are the
  highest-risk code and need property-style coverage (idempotent on `initial`,
  monotone for ratchet, etc.).
- **Translator:** `_eval_regime_state` / `_eval_ratchet` produce the expected
  Series on hand-built fixtures.
- **Trader:** `evaluate_stateful` determinism *given the same state input*;
  the §6.4 idempotency guard (re-evaluate the same candle twice → state
  advances once).

### 7.2 Integration tests

- **Regression — the backward-compat gate.** BB Breakout and Golden Cross
  (the two live-seeded v1 strategies) must validate **and produce
  byte-identical backtest results** to a baseline captured *before* any Phase
  A code. This is the load-bearing test — capture the baseline in A.1.
- **Turtle Trading — see the caveat.** The brief names Turtle as the
  end-to-end integration test ("extracts cleanly, runs end-to-end, non-zero
  trade count"). **Turtle's System-1 entry is skip-after-winner — T3.**
  Under the recommended T1+T2 scope (§9), Turtle's *channel breakout + 2N
  volatility stop + regime* portions extract and backtest, but the
  skip-after-winner filter does **not**. Two honest options:
  - **(Recommended)** Use **Supertrend trend-following** as the Phase A
    end-to-end integration strategy instead — it is a pure T2 regime strategy,
    fully achievable in the recommended scope, and equally "previously
    rejected".
  - Keep Turtle as the criterion and accept Phase A includes the §4.6 engine
    rewrite (then Phase A is multi-shot — §9).

### 7.3 Regression

`uv run pytest` (whole workspace) + `uv run pyright` clean. The CLAUDE.md
gotcha applies: a green unit suite does **not** prove the compose stack
works — the §10 acceptance criteria include a live end-to-end run.

---

## 8. Risk areas

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| 1 | **Numba `njit` scan kernels** (§4.3) — nopython mode is unforgiving; a kernel bug silently produces wrong signals | High | Plain-Python reference + property tests (§7.1); kernels are small and isolated in one module |
| 2 | **T3 / Turtle breaks `from_signals`** (§4.6, §0.3) | High | Scope T3 out of Phase A (§9); pick a T2 integration strategy (§7.2) |
| 3 | **Backtest⇄trader state-machine drift** (§6.6) — two implementations of one recurrence diverge | High | Share the recurrence in `marketmind_shared`; explicit parity test |
| 4 | **`reset="per_trade"` ratchet hidden circularity** (§4.4) | Medium | Ship `reset="never"` only; validator-reject `per_trade` until T3 path exists |
| 5 | **LLM extraction quality on novel tags** (§3) | Medium | Worked examples in prompt; baseline + watch `not_extractable` rate |
| 6 | **Walk-forward fold state discontinuity** (§5.2) | Medium | Warm-up prefix per fold; document as approximation |
| 7 | **Idempotent state advance on restart** (§6.4) | Medium | `updated_at_candle_close_ts` guard + dedicated test |
| 8 | **Schema migration lock-in** — if the v2 state representation is wrong, future schema changes are stuck | Medium | Inline/anonymous state, no named variables (§2.2); `ratchet`-as-expression composition (§1.4); `schema_version` literal widening keeps v1 frozen |
| 9 | **Prompt-cache invalidation** (§3.2) | Low | Append-only prompt edit; accept the one-time tool-block miss |

---

## 9. Realistic time estimate per sub-phase

Honest assessment of what is genuinely one-shot scale (a single focused
implement-review-iterate pass) and what is not.

| Sub-phase | Scope | One-shot? | Estimate |
|-----------|-------|-----------|----------|
| **A.1 Schema + validator** | `RatchetExpr`, `RegimeStateCondition`, `PriorTradeCondition` models; union wiring; `schema_version` widening; new validator rules; fixtures; JSON-Schema export | **Yes** | 1 session |
| **A.2 Extraction** | Prompt append-section + worked examples; tool `$defs`; cache verification; `not_extractable` baseline | **Yes** | 1 session |
| **A.3 Backtest engine — T1+T2** | `stateful.py` numba kernels; `translator` helpers; `StatefulDiagnostics`; regression baseline | **Borderline** — feasible as a one-shot but the riskiest; numba kernels need deep review | 1–2 sessions |
| **A.4 Overfitting** | Walk-forward continuous-run for stateful specs (§5.2); MC down-weight + interpretation note (§5.3); sweep needs no change (§5.1) | **Yes** | 1 session |
| **A.5 Trader stateful execution** | `0013` migration; `trader_strategy_state`; `StrategyState` DTO; `StatefulStrategyTemplate`; `signal_engine` wiring; idempotency guard | **Yes** — idempotency is the careful bit | 1–2 sessions |
| **A.6 Tier 3 — `prior_trade` / skip-after-winner** | `from_order_func` or iterative simulator; `prior_trade` engine support; `reset="per_trade"` ratchet | **No** — a backtest-engine architecture change; its own design pass | 3+ sessions, separate phase |

**Bottom line, stated plainly:** Phase A **as originally scoped** — including
skip-after-winner and Turtle end-to-end — **is not a single one-shot.** The
T1+T2 portion (A.1–A.5) *is* achievable as a sequence of tightly-reviewed
one-shot sub-phases. T3 (A.6) is a genuine engine rewrite and should be its
own phase with its own design doc.

**Recommendation:** Define **Phase A = A.1–A.5 (T1+T2)**. Keep
`PriorTradeCondition` in the schema as a forward-compatible, validator-gated
stub. Split skip-after-winner into **Phase A′** (or fold it into Phase D's
live-execution work, which already touches the order path). Choose a T2
integration strategy (Supertrend trend-following) for the Phase A acceptance
test in place of Turtle.

---

## 10. Acceptance criteria — Phase A complete (recommended T1+T2 scope)

Phase A is done when **all** of the following hold:

1. **Schema.** `RatchetExpr`, `RegimeStateCondition`, `PriorTradeCondition`
   exist; `schema_version` accepts `"1.0"` and `"2.0"`; the JSON-Schema bundle
   and TypeScript types regenerate cleanly.
2. **Backward compatibility (load-bearing).** All 12 existing golden fixtures
   validate unchanged. BB Breakout and Golden Cross produce **byte-identical
   backtest results** to the pre-Phase-A baseline (same metrics, same trade
   count, same equity curve).
3. **Validator.** Every new `error_code` in §2.1 fires on its invalid fixture;
   a v2 element in a `schema_version="1.0"` spec is rejected.
4. **Extraction.** A T2 strategy (Supertrend trend-following) extracts to a
   valid `schema_version="2.0"` spec with a `regime_state` condition. The
   `not_extractable` rate on the existing strategy corpus is **not worse**
   than the pre-Phase-A baseline.
5. **Backtest.** That T2 strategy backtests with a **non-zero trade count**;
   `StatefulDiagnostics` is populated; the regime is shown to latch/unlatch.
6. **Overfitting.** Walk-forward, parameter-sweep, and Monte-Carlo run on the
   T2 strategy without error; walk-forward uses the continuous-run path for
   stateful specs (§5.2).
7. **Trader.** The `0013` migration applies; a stateful version can be seeded,
   approved, and evaluated live; `trader_strategy_state` rows are written
   atomically with signals; re-evaluating the same candle advances state
   exactly once (idempotency test passes).
8. **Parity.** A dedicated test proves the backtest scan and the live
   `evaluate_stateful` produce the **identical** state trajectory on the same
   candle data (§6.6).
9. **Regression.** The full suite — all 763 `test_` functions (~800 with
   parametrized expansions) — passes; `uv run pyright` is clean; no `Any`/`any`
   introduced.
10. **End-to-end.** The compose stack runs one stateful strategy through
    ingest → extract → backtest → overfitting → seed → approve → one live
    signal cycle, with no error (the CLAUDE.md lesson: a green unit suite does
    not prove the stack composes).

**Explicitly out of scope for Phase A** (tracked, not done): `prior_trade`
engine support, `ratchet(reset="per_trade")`, skip-after-winner, and therefore
a fully end-to-end Turtle System-1. These move to Phase A′ / a later phase
(§9).

---

## Appendix — open questions for the reviewer

1. **Scope decision (§9):** approve Phase A = T1+T2 (A.1–A.5), with T3 split
   out? Or require T3 in Phase A and accept it is multi-shot?
2. **Integration strategy (§7.2):** Supertrend trend-following instead of
   Turtle for the Phase A acceptance test?
3. **`reset="per_trade"` ratchet (§4.4):** ship the field validator-gated, or
   omit the field from the v2.0 schema entirely until the T3 engine exists?
4. **Schema descriptions (§3.4):** prompt-prose teaching only for Phase A, or
   also invest in the `export_json_schema.py` description-emit change now?
