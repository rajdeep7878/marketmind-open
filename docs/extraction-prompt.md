# Phase 2.2 — Extraction prompt design

This document explains the design of the Phase 2.2 extraction prompt
that lives in `workers/src/marketmind_workers/services/extraction_prompt.py`
as `EXTRACTION_SYSTEM_PROMPT`. The executable form (the constant) and
this doc must stay in lockstep: a byte-level change in the constant
invalidates the Anthropic prompt cache, so we treat every word like a
sacred design artefact.

## What the prompt produces

A single tool call to `submit_extraction` whose input is

```jsonc
{
  "spec":   <StrategySpec object> | null,
  "report": <ExtractionReport object>      // always present
}
```

The schema for `spec` is loaded from the committed Phase 1 bundle
(`web/src/types/generated/schemas.json`), so the model sees exactly
what `validate_spec` will accept. The `report` shape is defined by
Pydantic in `shared/.../schemas/extraction_report/`. Both halves are
required regardless of verdict: even refusals carry a report
explaining *why*.

## The four verdicts

| Verdict                  | `spec`     | When                                                                                |
|--------------------------|------------|-------------------------------------------------------------------------------------|
| `fully_extractable`      | non-null   | All critical fields present with high confidence; spec validates cleanly            |
| `partially_extractable`  | non-null   | Critical fields present but some are defaulted/inferred; spec still validates       |
| `not_extractable`        | `null`     | Source describes a strategy but its rules require human judgment (drawn levels etc.)|
| `not_a_strategy`         | `null`     | Source is commentary, news, mindset content, or pure promotion                      |

## Design philosophy

Three load-bearing decisions:

### 1. Brutal honesty over reluctant extraction

Most strategy content on the internet is discretionary trading wearing
a strategy costume. The prompt explicitly tells the model:

> You are NOT here to be encouraging or to help the trader. You are
> here to protect users from spending real money on strategies whose
> rules are too vague to be tested. Brutal honesty is the value you
> provide.

Refusing a vague strategy is more valuable than over-extracting one,
because an over-extracted spec produces a backtest the user will trust
even though it doesn't reflect what they'd actually do live. The model
is told to err toward refusal whenever the rules require human
judgment.

### 2. Dual output: spec AND report, always

A traditional extraction pipeline returns either a parsed object or an
error. We return both:

- The **spec** is the executable artefact the backtester consumes.
- The **report** is the human-readable explanation: every rule the LLM
  saw, its confidence, the source quotes, the verdict reasoning, and
  (for refusals) the explanation.

This solves two problems at once. The user understands *why* a
refusal happened, and the partially-extractable case becomes actionable
— "here's the spec, here are the missing fields, here's why your
backtest will be approximate."

### 3. Calibrated confidence

Every rule and the overall verdict have a confidence in [0.0, 1.0].
The prompt anchors these to specific evidence levels:

- 0.95–1.0 — source stated this with a specific number/value
- 0.75–0.95 — stated clearly but with mild ambiguity
- 0.50–0.75 — inferred from context with reasonable confidence
- 0.25–0.50 — guessed; user should review
- 0.0–0.25 — no real basis; placeholder

This matters because **calibration is what makes the dashboard
trustworthy** in Phase 5. If 0.95 means "the source literally said
8%" and 0.6 means "this is a guess", the UI can dispatch on that.

## Red-flag list

The prompt includes an explicit list of patterns that almost always
indicate a non-backtestable strategy:

- Manually marking the chart / drawing support and resistance
- "Voila" / "you'll know when you see it"
- ICT terminology (order blocks, FVGs, liquidity sweeps, etc.)
- SMC smart-money concepts when used as the strategy basis
- Harmonic patterns (Gartley, Bat, Cypher, etc.)
- Elliott Wave counting
- Generic "confluence" / "trust your gut"

Mention of any of these doesn't automatically force a refusal — the
test is whether the strategy *depends* on that subjective layer. A
strategy that says "I avoid trading near major Fib levels" is fine if
its actual entry/exit are mechanical.

## Edge cases the prompt handles explicitly

- **Multiple variants tested**: extract the best/recommended variant,
  note the others in `extraction_notes`.
- **Optimized parameters**: extract them with confidence 0.7–0.85 and
  add an extraction_note flagging overfit risk.
- **Unstated essentials**: don't invent defaults for `costs` or
  `position_sizing` — omit them and let our defaults apply
  server-side. The model is told that inventing reasonable defaults
  in this tool is *dishonest*.
- **Sales-heavy videos**: ~70% of YouTube trading videos are sales.
  The prompt tells the model to filter and refuse if no strategy
  content remains.
- **Direction inference**: bullish-only signals (Golden Cross, RSI <
  30) → `long`. Bearish-only → `short`. Symmetric → `long` with a
  note that short mirrors.

## Prompt caching

The system prompt and the tool definition are both marked
`cache_control: ephemeral`. Anthropic caches each block once the
combined cacheable content crosses ~1024 tokens. The prompt + tool
schema together are ~22k tokens, so the cache is always engaged.

Cache pricing (Sonnet 4.6):
- Cache write: 1.25x normal input rate
- Cache read: 0.1x normal input rate

On the first extraction in a 5-minute window, we pay the write
premium (~$0.083). Every subsequent extraction reads from cache for
~$0.007 instead of paying the full input rate (~$0.067). The
`extract_strategy` job's lru_cache on the tool builder keeps the
key stable across calls.

## When to change the prompt

The prompt is intentionally rigid. Treat changes as a real design
decision:

1. Edit `EXTRACTION_SYSTEM_PROMPT` in
   `workers/src/marketmind_workers/services/extraction_prompt.py`.
2. Update this doc to match.
3. Re-run the manual test harness (see commit history) against both
   test transcripts (MambaFx + Quant Tactics) to confirm behaviour
   hasn't regressed.
4. Expect a cache miss on the first extraction after the change —
   that's normal.

The constant is held as a single string (not assembled at runtime)
specifically so the cache key is stable and the prompt is easy to
diff in PRs.

## Change log

- **2026-06-06 (Phase E.3):** added a "Multi-leg / market-neutral /
  perpetual-pair spreads" teaching block to `## SCHEMA REFERENCES`. It
  teaches the multi-leg spec shape (`instrument` = leg A, `legs[]` =
  additional legs with per-leg `direction` + `weight`, `spread` config with
  `method`/`zscore_period`/`entry_z`/`exit_z`/`corr_*`), the `crypto_perp`
  asset class for perpetual swaps (funding handled by the engine from data,
  not modelled in the spec beyond an optional `costs.funding_model` note),
  a field-name-bleed guard (spread `zscore_period`/`entry_z`/`exit_z` are NOT
  the single-leg ZScoreCondition's `period`/`threshold`/`form`; `weight` is a
  notional ratio not a percent; per-leg `direction` lives on `legs[]`), and a
  worked BTC/ETH log-spread example. Single-symbol strategies leave `legs`
  and `spread` absent. Invalidates the prompt cache once (expected).

## The actual prompt text

The canonical prompt lives at
`workers/src/marketmind_workers/services/extraction_prompt.py` —
`EXTRACTION_SYSTEM_PROMPT`. Copying it here would duplicate the cache
key in two places and risk drift. Read it there.
