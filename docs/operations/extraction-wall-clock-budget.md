# Extraction wall-clock budget too tight after token-ceiling raise (2026-05-19)

## TL;DR

Earlier this session we raised `MAX_OUTPUT_TOKENS` from 4096 → 16384 to fix truncation refusals. That uncovered a downstream bottleneck: extractions producing fully-populated payloads were taking 60-70 seconds to generate and tripping `MAX_WALL_CLOCK_SECONDS = 60.0`. Sonnet 4.6 generates roughly 60-80 tokens/sec on the API, so 16k tokens worst-case can take 3-4 minutes legitimately. Raised the budget to **240s** with corresponding RQ-timeout bump to 360s.

## How the bottleneck surfaced

A Turtle Trading article (`https://www.altrady.com/blog/crypto-trading-strategies/turtle-trading-strategy-rules`) extraction completed cleanly with the new token ceiling but the budget guard fired post-call because generation took ~67s. The error was `ExtractionTimeoutError: extraction exceeded 60.0s wall-clock budget`. Same pattern would have hit any rules-dense article producing 4k+ output tokens.

The deadline check sits AFTER the SDK call returns (it's not a hard timeout), so the call always completes — we just throw away the result if elapsed > budget. So we paid for the full $0.12 call and got an exception instead of the result, twice (once on the timed-out URL, then again every time the user re-ran). The fix doesn't change the call's cost or behaviour; it just stops discarding legitimately-long generations.

## Why 240s

| Component | Time |
|----------|------|
| Sonnet 4.6 generation rate | ~60-80 tokens/sec |
| Max output tokens | 16384 |
| Worst-case generation | 16384 / 60 ≈ 273s |
| Worst-case with cache hit on input | 16384 / 80 ≈ 205s |
| Practical median observed | 60-90s |
| Budget chosen | **240s** |

240s sits between the cached and uncached worst-case for the new token ceiling. If a single call genuinely exceeds 240s, something is wrong (model stuck, SDK hung, etc.) and we want the deadline guard to trigger — that's its purpose.

## Layered timeouts

There are three timeouts in play. Each must be greater than the one it wraps, otherwise the outer one fires first and the inner one's clean error path never runs.

| Layer | Timeout | Where set |
|------|---------|-----------|
| Anthropic SDK default | (~10 min — much higher) | SDK internal |
| In-extract deadline guard | **240s** | `extract.py:MAX_WALL_CLOCK_SECONDS` |
| RQ `job_timeout` for `EXTRACT_STRATEGY` | **360s** | `api/routes/strategies.py` |

The RQ→guard margin is 120s. That covers pre-flight cost check + extraction service entry overhead + payload validation + cost-row + extraction-row DB writes + queue housekeeping. Empirically those steps total well under 1s, so 120s is generous.

The earlier 300s RQ timeout (already present from the transcribe-job-timeout work in this session) had no headroom over a 240s guard — would have killed a 240s budget call before the worker could produce a clean error. Bumped to 360s.

## How we caught it

Same verification-first pattern as the `stop_reason` incident:

1. Added `generation_seconds` to the existing `extraction_first_call_complete` and `extraction_retry_call_complete` structlog events (rounded to 2 decimal places).
2. Included `generation_seconds` in the `ExtractionTimeoutError` message itself ("first call took {X:.1f}s") so the refusal is self-describing.
3. Re-ran the Turtle Trading URL with the new budget; logs immediately surfaced `generation_seconds=66.72` — exactly the kind of normal-but-slow extraction the old 60s budget was incorrectly killing.

## Changes

| File | Change |
|------|--------|
| `workers/src/marketmind_workers/services/extract.py` | `MAX_WALL_CLOCK_SECONDS: 60.0 → 240.0` with multi-line comment documenting the 2026-05-19 incident + the token-rate math. `first_call_start` / `retry_call_start` timing capture; `generation_seconds` added to both `extraction_first_call_complete` and `extraction_retry_call_complete` structlog events. `ExtractionTimeoutError` messages now include the timing breakdown. |
| `api/src/marketmind_api/routes/strategies.py` | RQ `job_timeout`: 300 → 360 with comment explaining the layering (must exceed `MAX_WALL_CLOCK_SECONDS`). |
| `workers/tests/test_extract.py` | New `test_extract_first_call_overruns_budget_raises_timeout`: monkeypatches `MAX_WALL_CLOCK_SECONDS` to 10ms and uses a slow `_FakeAnthropic` that `time.sleep`s past the budget. Verifies the deadline guard fires post-call (not a hard timeout) and the error mentions "wall-clock budget". |

## What "future you" should do if this rhymes

If extractions start failing with `ExtractionTimeoutError`:

1. Read the message — it now includes `first call took X.Xs` (and retry timing if the retry ran). Compare to `MAX_WALL_CLOCK_SECONDS`.
2. If the timing is just over the budget on a long article: raise the budget. Bump `MAX_WALL_CLOCK_SECONDS` AND `job_timeout` together. Costs don't change (we pay for the same call regardless of whether we discard the result).
3. If timing is ≫ budget (e.g., 1000s on a small article): something is genuinely wrong. Check `extraction_first_call_complete` logs for `stop_reason` — `"refusal"` or `"end_turn"` with low `output_tokens` and high `generation_seconds` would suggest a stuck SDK call or upstream API issue, not a legitimately long generation.
4. If `extraction_first_call_complete` doesn't appear at all but the timeout fired: the SDK call itself stalled before returning. Suspect network issues to the Anthropic API or SDK transport bugs.

## Hard-won knowledge worth adding to CLAUDE.md

> **Three layered timeouts must be ordered correctly.** Whenever we tighten an inner timeout, audit the outer ones. The order is `Anthropic SDK default > MAX_WALL_CLOCK_SECONDS > RQ job_timeout reversed` — outer wraps inner, and the RQ timeout must be the LARGEST of the three (it's the outermost wrapper, not the innermost). If RQ kills the job first, the worker's structured error path never runs and the user gets a generic "JobTimeoutException" with no context. Always leave at least 60-120s headroom between each layer for pre-flight checks, persistence, and queue overhead.

> **The wall-clock guard is a post-call deadline, not a hard timeout.** Raising `MAX_WALL_CLOCK_SECONDS` does NOT increase risk of dangling state — we never kill mid-stream. The SDK call always completes; we just discard the result if elapsed > budget. So the only cost of raising the budget is "letting longer-running calls succeed instead of being thrown away".

## Incident-affected extraction

For audit trail:

| extraction_id | source | timing | created_at |
|---|---|---|---|
| (timed-out call — no row persisted; user re-ran) | altrady turtle trading | `generation_seconds=66.72` would have been the value if logged | 2026-05-19 23:55-ish |
| `6166a6de-4b39-45e3-9019-5d2ffce59ef0` | altrady turtle trading (verification re-run) | `generation_seconds=66.72`, `output_tokens=4341`, content verdict not_extractable with 0.72 confidence + 16 extracted_rules | 2026-05-19 23:56:07 |

The verification re-run is the "Patient Zero" record. The Turtle Trading content is a textbook case for this fix: rules-dense, every parameter explicit (Donchian breakout periods, ATR-based sizing, 2-ATR stops, pyramiding rules). The model rightly spent time enumerating all 16 extracted_rules + 11 backtestable_parts. That's exactly the kind of high-value extraction the old budget was killing.

## Relationship to the earlier `stop_reason=max_tokens` incident

This incident is the direct downstream consequence of `docs/operations/extraction-stop-reason-max-tokens.md`. The sequence:

1. **Earlier today:** `MAX_OUTPUT_TOKENS = 4096` truncated extractions mid-payload. Six refusals, no useful output.
2. **Fix:** raised to 16384.
3. **New ceiling now reachable:** the model can now generate substantially more output per call.
4. **This incident:** the wall-clock budget, calibrated for the old 4096-token ceiling, no longer fits the legitimate generation time of full extractions.
5. **Fix:** raise the wall-clock budget to match the new token budget. Raise the RQ timeout to preserve the layering.

The general lesson: **raising one limit will surface the next-binding constraint downstream**. Same shape will probably recur if/when we raise `MAX_EXTRACTION_USD` next (the next binding limit) — cost-cap incidents will become the new failure mode. Worth knowing.
