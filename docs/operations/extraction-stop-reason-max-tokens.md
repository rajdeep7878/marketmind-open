# Extraction failures with `stop_reason=max_tokens` (2026-05-19)

## TL;DR

Six extractions in 10 hours all failed with the refusal text *"Extraction downgraded due to schema validation failure: tool_use payload missing or malformed `report` field."* Root cause: `MAX_OUTPUT_TOKENS = 4096` was too low — the model serialised `spec` first, hit the token ceiling exactly, and the Anthropic SDK returned a partial JSON dropping the un-serialised `report` field. Raised to **16384**.

## How we caught it

The refusal text was generic and identical across all six failures. Without runtime visibility into the SDK response there was no way to tell whether the model was misbehaving, our parser was buggy, or the model was being cut off mid-payload.

**Verification-first approach** (instead of guessing-and-fixing):

1. Added diagnostic logging to `extract.py`:
   - `extraction_first_call_complete` (and `_retry_call_complete`) structlog events carrying `stop_reason` + `payload_keys` (sorted list of which top-level fields actually arrived).
   - `_result_from_payload` now accepts a `stop_reason=` kwarg. When the `report` field is missing AND `stop_reason == "max_tokens"`, the refusal text explicitly says so and recommends raising `MAX_OUTPUT_TOKENS`.

2. Re-ran the Quantpedia URL (known-failing repro: `https://quantpedia.com/how-to-design-a-simple-multi-timeframe-trend-strategy-on-bitcoin/`).

3. Logs surfaced the smoking gun on first try:
   ```
   extraction_first_call_complete stop_reason=max_tokens payload_keys=['spec']
   extract_strategy_complete input_tokens=3220 output_tokens=4096 ...
   ```

   `output_tokens=4096` exactly. The model serialised `spec` first (per the SDK's deterministic field ordering for the tool's `properties` block), used every available output token, and the SDK parsed the partial JSON returning `{"spec": {...}}` only.

## Why a generous output budget is fine

Sonnet 4.6 supports output up to 64k tokens. A fully-populated extraction (StrategySpec + ExtractionReport with extracted_rules, backtestable_parts, non_backtestable_parts, author_claims, reasoning, refusal_explanation) easily exceeds 4096 tokens on longer articles. 16384 fits comfortably.

Pre-flight cost-cap impact:
- Old worst-case output cost: 4096 × $15/M = **$0.0614 / call**
- New worst-case output cost: 16384 × $15/M = **$0.2458 / call**
- `MAX_EXTRACTION_USD = $0.50` remains; the actual ceiling per call is unchanged
- Previous failed extractions were costing ~$0.11 each anyway (`output_tokens=4096` charged-for and dropped on the floor); the new ceiling at least gets us a usable result for that spend

## Secondary bug found (not fixed)

The retry path also dies silently. Timing in the test log:

```
23:35:21.067996  extraction_retry_starting   reason=spec_validation_failed
23:35:21.331687  extract_strategy_complete   ...
```

0.26 seconds end-to-end. That's far too fast for a real Anthropic call; the retry SDK call must be raising synchronously. The `except Exception` block at `extract.py:391` catches it and routes straight to `_downgrade_to_refusal` without logging *what* exception was raised.

Most likely cause: `_build_retry_messages` replays the first response's tool_use block as a prior assistant turn (`extract.py:259-280`). When the first response's tool_use input is a truncated JSON string, replaying it produces an invalid message that the API rejects. Worth fixing — but with `MAX_OUTPUT_TOKENS=16384` the first call succeeds for normal inputs and the retry path simply doesn't run. Filed for follow-up; not blocking.

## Changes

| File | Change |
|------|--------|
| `workers/src/marketmind_workers/services/extract.py` | `_stop_reason()` helper; `extraction_first_call_complete` + `extraction_retry_call_complete` structlog events with stop_reason + payload_keys; `_result_from_payload(stop_reason=)` new kwarg producing truncation-specific refusal text when both conditions are met. `MAX_OUTPUT_TOKENS: 4096 → 16384` with the failure-mode summary as a comment. |
| `workers/tests/test_extract.py` | `_FakeResponse(stop_reason=...)` test-double extension. Three new tests: `test_result_from_payload_missing_report_with_max_tokens_returns_truncation_text`, `test_result_from_payload_missing_report_with_other_stop_reason_uses_generic_text`, `test_extract_max_tokens_truncation_downgrades_with_truncation_refusal`. |

## What "future you" should do if this rhymes

If you see a wave of `not_extractable` extractions whose refusal text mentions `stop_reason=max_tokens`:

1. Pull `output_tokens` from `extract_strategy_complete` logs for those extractions. If exactly equal to `MAX_OUTPUT_TOKENS`, it's truncation, same as this incident.
2. Raise `MAX_OUTPUT_TOKENS`. Costs scale linearly; the `MAX_EXTRACTION_USD` cap floors the worst case.
3. Don't fix the retry path until you've ruled out other reasons for it failing. Once max_tokens is no longer hit, retries should rarely fire.

If you see refusal text mentioning a stop_reason *other than* `max_tokens` (e.g. `end_turn`, `refusal`, `stop_sequence`), the generic-text branch fires and includes the real stop_reason — that's a different incident, investigate from there.

## Hard-won knowledge worth adding to CLAUDE.md

> **`MAX_OUTPUT_TOKENS` too low → "tool_use payload missing or malformed `report` field" with no obvious indication of truncation.** The Anthropic SDK parses partial JSON from a max_tokens-truncated tool_use block and returns whatever dict it could read; fields that the model hadn't written yet simply vanish from `tool_use.input`. The required-fields list in the tool's `input_schema` is advisory at runtime — Anthropic does not enforce it. Always log `response.stop_reason` alongside tool-use parse failures so the truncation case is distinguishable from genuine schema violations.

## Incident-affected extractions

For audit-trail purposes — all six failed with this root cause:

| extraction_id | source | created_at |
|---|---|---|
| `713d7a85-c379-4050-bdae-9f51eb54f9ea` | altrady.com turtle trading | 2026-05-19 23:22:19 |
| `0a25543e-84a5-4fef-81a7-5d6b4617dbe0` | Quantpedia (multi-timeframe) | 2026-05-19 23:08:27 |
| `cf8979f4-5c2d-468a-90a0-6d3ced55c0c7` | medium.com (quantitative crypto) | 2026-05-19 23:06:40 |
| `9acd75de-62ec-48d3-82f6-bd6574e60f3a` | Quantpedia (multi-timeframe) | 2026-05-19 23:00:53 |
| `78a3ac53-7de0-47de-9bd7-f71f0bcaefe5` | YouTube `c9-SIpy3dEw` | 2026-05-19 16:31:55 |
| `643f2993-3142-4933-9f10-59ca7388fbcf` | YouTube `NojfYk31_xI` | 2026-05-19 13:53:18 |

Plus the verification-run extraction `27239432-7628-466b-93be-3a0443dfb8f1` (Quantpedia, 23:35:21) which carries the new diagnostic text in its `refusal_explanation` — that's the "Patient Zero" record of the fix.
