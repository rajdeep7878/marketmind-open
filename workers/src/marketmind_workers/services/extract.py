"""LLM-driven StrategySpec extraction.

`extract_strategy(transcript, source)` is the public entry point. It:

  1. Builds the user message from (source_url, source_type, full_text).
  2. Calls the Anthropic Messages API with:
       - model: claude-sonnet-4-6
       - system: EXTRACTION_SYSTEM_PROMPT (cache_control: ephemeral)
       - tools: [submit_extraction] (cache_control: ephemeral)
       - tool_choice: force submit_extraction
  3. Parses the tool_use input.
  4. If `spec` is non-null, validates it via Phase 1's `validate_spec`.
     On validation failure, retries ONCE with the errors fed back to
     the model. If still failing, downgrades verdict to NOT_EXTRACTABLE
     and folds the errors into refusal_explanation.
  5. Returns ExtractionResult(spec, report) plus a UsageStats record
     so the caller can persist cost data.

Error types:
  - ExtractionFailedError: API/tool call failed after both attempts.
  - ExtractionTooExpensiveError: pre-flight token estimate > limit.
  - ExtractionTimeoutError: 60s wall-clock budget exceeded.

Tests mock the Anthropic client; one integration test hits the real
API with a tiny synthetic transcript.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Final

import structlog
from marketmind_shared.schemas import (
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
    Transcript,
    validate_spec,
)
from marketmind_shared.schemas.content import ExtractionInput
from marketmind_shared.schemas.strategy_spec import (
    StrategySpec,
    StrategySpecValidationErrorGroup,
)

from marketmind_workers.services.extraction_prompt import (
    build_submit_extraction_tool,
    build_user_message,
    system_prompt_blocks,
)

log = structlog.get_logger(__name__)

# ---- Public constants -------------------------------------------------------

EXTRACTION_MODEL: Final[str] = "claude-sonnet-4-6"
# 16k output tokens. Raised from 4096 on 2026-05-19 after a six-extraction
# pile-up of "tool_use payload missing or malformed `report` field"
# refusals — verified via diagnostic logging that stop_reason=max_tokens
# was hitting on every failure: the model serialised `spec` first, hit
# the 4096 ceiling, and the SDK dropped the un-serialised `report` field.
# Sonnet 4.6 supports up to 64k output tokens; 16k is comfortable
# headroom for a fully-populated StrategySpec + ExtractionReport with
# author_claims + extracted_rules. Pre-flight max-cost estimate rises
# from $0.06 to $0.25 — still well inside MAX_EXTRACTION_USD = $0.50.
# See docs/operations/extraction-stop-reason-max-tokens.md.
MAX_OUTPUT_TOKENS: Final[int] = 16384
MAX_EXTRACTION_USD: Final[float] = 0.50
# 240s wall-clock budget. Raised from 60s on 2026-05-19 after the
# MAX_OUTPUT_TOKENS bump from 4096 → 16384 surfaced a downstream
# bottleneck: long articles legitimately need more time to generate a
# fully-populated extraction. Sonnet 4.6 generates roughly 60-80
# tokens/sec on the API, so 16k tokens worst-case ≈ 3-4 minutes. 240s
# gives comfortable headroom. The check is a post-call deadline guard
# (not a hard timeout), so SDK calls always complete cleanly — we
# never kill mid-stream. The RQ `job_timeout` for EXTRACT_STRATEGY in
# `api/routes/strategies.py` must stay above this budget so RQ doesn't
# kill the job before the deadline guard can produce a clean error.
# See docs/operations/extraction-wall-clock-budget.md.
MAX_WALL_CLOCK_SECONDS: Final[float] = 240.0

# Anthropic Sonnet 4.6 pricing as of writing. Used for both pre-flight
# guards and post-hoc cost stamping. If the rate card changes, update
# the constants here (only one place).
_PRICE_INPUT_PER_M: Final[float] = 3.0
_PRICE_OUTPUT_PER_M: Final[float] = 15.0
# Anthropic prompt-caching pricing: cache writes cost 1.25x normal input
# tokens; cache reads cost 0.1x. The 0.1x read price is what makes this
# worth doing at all.
_PRICE_CACHE_WRITE_PER_M: Final[float] = 3.75
_PRICE_CACHE_READ_PER_M: Final[float] = 0.30

# Per-character estimate used in the pre-flight guard. This is rough on
# purpose: we want to refuse 100,000-char transcripts before paying for
# the API call, not enforce an exact token count. 4 chars/token is the
# documented Claude average for English prose.
_CHARS_PER_TOKEN_ESTIMATE: Final[int] = 4


# ---- Error types ------------------------------------------------------------


class ExtractionError(Exception):
    """Base for any failure raised by the extraction service."""


class ExtractionFailedError(ExtractionError):
    """API call or tool-use parsing failed twice."""


class ExtractionTooExpensiveError(ExtractionError):
    """Estimated cost (input + output ceiling) exceeds MAX_EXTRACTION_USD."""


class ExtractionTimeoutError(ExtractionError):
    """API call did not return within MAX_WALL_CLOCK_SECONDS."""


# ---- Usage stats ------------------------------------------------------------


@dataclass(frozen=True)
class UsageStats:
    """Token + cost record for a single extraction call.

    `cached_tokens` is the count of input tokens that hit the prompt
    cache (so we paid the 0.1x rate). The remaining input_tokens were
    either new (1x) or cache writes (1.25x). The Anthropic SDK exposes
    separate counters; we collapse the cache-write vs non-cache split
    into a single `input_tokens` field because the dashboard view
    doesn't need to split them.
    """

    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    estimated_usd: float


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    cached_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Compute the dollar cost of a call from its token counts.

    `input_tokens` here is the count Anthropic reports as the new
    (non-cache) input portion. Cache writes and cache reads are
    separated because they're priced differently.
    """
    base_input = max(0, input_tokens - cache_write_tokens)
    cost = (
        base_input / 1_000_000 * _PRICE_INPUT_PER_M
        + cache_write_tokens / 1_000_000 * _PRICE_CACHE_WRITE_PER_M
        + cached_read_tokens / 1_000_000 * _PRICE_CACHE_READ_PER_M
        + output_tokens / 1_000_000 * _PRICE_OUTPUT_PER_M
    )
    return round(cost, 6)


def _preflight_cost_check(transcript_chars: int) -> None:
    """Refuse extractions that are obviously too expensive.

    A 4-hour video transcribes to ~50k chars (~12k tokens at 4 chars/tok).
    A 100k-char input + 4k output is well under $0.50, so the limit
    really only catches pathological inputs. We compute a conservative
    upper bound assuming NO cache hits and a fully-populated output.
    """
    estimated_input_tokens = math.ceil(transcript_chars / _CHARS_PER_TOKEN_ESTIMATE)
    # Add a fixed overhead for the system prompt + tool schema, which
    # together come to roughly 20k tokens (uncached). On cache hits the
    # actual cost is far lower; this is the worst case.
    estimated_input_tokens += 20_000
    upper_bound_usd = _estimate_cost(estimated_input_tokens, MAX_OUTPUT_TOKENS)
    if upper_bound_usd > MAX_EXTRACTION_USD:
        raise ExtractionTooExpensiveError(
            f"estimated upper-bound cost ${upper_bound_usd:.4f} exceeds "
            f"per-call ceiling ${MAX_EXTRACTION_USD:.2f} "
            f"(transcript {transcript_chars} chars)",
        )


# ---- Anthropic client factory ----------------------------------------------


def _make_anthropic_client() -> Any:
    """Build a default Anthropic client. Pulled out so tests can mock it.

    Import is local to the function so importing this module doesn't
    pull in the SDK at collection time; that keeps `pyright` and the
    "no anthropic in prod" check honest about *where* the SDK lands.
    """
    from anthropic import Anthropic

    return Anthropic()


# ---- Internal call helpers -------------------------------------------------


def _extract_tool_use_payload(content: list[Any]) -> dict[str, Any] | None:
    """Pull the submit_extraction tool_use block from a response.

    Returns the input dict if found, else None. The shape of `content`
    is the SDK's list of typed blocks; we duck-type on `.type` and
    `.name`/`.input` so the same code works against the real SDK and
    test doubles.
    """
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == (
            "submit_extraction"
        ):
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                return payload
    return None


def _stop_reason(response: Any) -> str:
    """Best-effort read of the Anthropic response stop_reason.

    Returns the literal value (e.g. "end_turn", "tool_use",
    "max_tokens", "stop_sequence", "refusal") or the string
    "unknown" if the field is absent. The string-typed return makes
    this safe to drop into structlog events directly.
    """
    val = getattr(response, "stop_reason", None)
    if val is None:
        return "unknown"
    return str(val)


def _usage_from_response(response: Any) -> tuple[int, int, int, int]:
    """Return (input_tokens, output_tokens, cached_read, cache_write).

    SDK shapes vary slightly across versions. We probe the attributes
    we know about; missing ones default to 0.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return (0, 0, 0, 0)
    in_t = int(getattr(usage, "input_tokens", 0) or 0)
    out_t = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return (in_t, out_t, cache_read, cache_write)


def _build_request_kwargs(
    user_message: str,
    *,
    prior_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the Messages API request kwargs.

    `prior_messages` is used on the retry path to feed back validation
    errors. The user-message + assistant-tool-use + tool-result trio
    lets us keep prompt caching working across the retry.
    """
    messages: list[dict[str, Any]] = list(prior_messages or [])
    if not messages:
        messages.append({"role": "user", "content": user_message})
    return {
        "model": EXTRACTION_MODEL,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system_prompt_blocks(),
        "tools": [build_submit_extraction_tool()],
        "tool_choice": {"type": "tool", "name": "submit_extraction"},
        "messages": messages,
    }


def _build_retry_messages(
    user_message: str,
    first_response: Any,
    first_payload: dict[str, Any] | None,
    error_text: str,
) -> list[dict[str, Any]]:
    """Construct messages for the retry after a validation failure.

    Replays the model's first tool_use as a normal assistant turn so
    the conversation stays consistent, then injects a synthetic user
    message describing what went wrong. The model gets to try again
    from there with the same tool definition.
    """
    assistant_turn = []
    for block in first_response.content:
        btype = getattr(block, "type", None)
        if btype == "tool_use":
            assistant_turn.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", "first_attempt"),
                    "name": getattr(block, "name", "submit_extraction"),
                    "input": first_payload or {},
                },
            )
        elif btype == "text":
            assistant_turn.append({"type": "text", "text": getattr(block, "text", "")})

    return [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": assistant_turn},
        {
            "role": "user",
            "content": (
                "The previous extraction failed validation. Issue:\n\n"
                f"{error_text}\n\n"
                "Re-submit via the submit_extraction tool. If the spec "
                "cannot be fixed cleanly, set spec to null, set verdict "
                "to not_extractable, and explain in refusal_explanation."
            ),
        },
    ]


# ---- Main entry point ------------------------------------------------------


def extract_strategy(
    transcript: Transcript,
    source: ExtractionInput,
    *,
    client: Any | None = None,
) -> tuple[ExtractionResult, UsageStats]:
    """Run the LLM extraction pipeline against `transcript`.

    Returns (result, usage). The caller (job in workers/jobs/) is
    responsible for persisting both.

    On validation failure of a non-null spec, we retry once with the
    errors fed back to the model. If the retry still fails to produce
    a valid spec, we downgrade the result to NOT_EXTRACTABLE and fold
    the validation errors into refusal_explanation rather than raising
    — the report from the first attempt is still useful to the user.
    """
    _preflight_cost_check(len(transcript.full_text))

    sdk_client = client if client is not None else _make_anthropic_client()
    user_message = build_user_message(
        source.source_url,
        source.source_type,
        transcript.full_text,
    )

    start = time.monotonic()

    # ---- First call --------------------------------------------------------
    request_kwargs = _build_request_kwargs(user_message)
    first_call_start = time.monotonic()
    try:
        response = sdk_client.messages.create(**request_kwargs)
    except Exception as exc:  # broad on purpose — any SDK exception
        raise ExtractionFailedError(f"Anthropic API call failed: {exc}") from exc
    first_generation_seconds = time.monotonic() - first_call_start

    if time.monotonic() - start > MAX_WALL_CLOCK_SECONDS:
        raise ExtractionTimeoutError(
            f"extraction exceeded {MAX_WALL_CLOCK_SECONDS}s wall-clock budget "
            f"(first call took {first_generation_seconds:.1f}s)",
        )

    payload = _extract_tool_use_payload(response.content)
    first_stop_reason = _stop_reason(response)
    log.info(
        "extraction_first_call_complete",
        stop_reason=first_stop_reason,
        payload_keys=sorted(payload.keys()) if isinstance(payload, dict) else None,
        payload_is_none=payload is None,
        generation_seconds=round(first_generation_seconds, 2),
    )
    if payload is None:
        raise ExtractionFailedError(
            "Anthropic response contained no submit_extraction tool_use block",
        )

    in_t, out_t, cache_read, cache_write = _usage_from_response(response)

    result_or_retry = _result_from_payload(payload, stop_reason=first_stop_reason)
    if isinstance(result_or_retry, ExtractionResult):
        usage = UsageStats(
            model=EXTRACTION_MODEL,
            input_tokens=in_t,
            output_tokens=out_t,
            cached_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_usd=_estimate_cost(
                in_t,
                out_t,
                cached_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
        )
        if usage.estimated_usd > MAX_EXTRACTION_USD:
            log.warning(
                "extraction_over_budget",
                estimated_usd=usage.estimated_usd,
                limit=MAX_EXTRACTION_USD,
            )
        return result_or_retry, usage

    # Retry path: result_or_retry is the validation error text.
    validation_error_text = result_or_retry
    log.info("extraction_retry_starting", reason="spec_validation_failed")

    retry_messages = _build_retry_messages(
        user_message,
        response,
        payload,
        validation_error_text,
    )
    retry_kwargs = _build_request_kwargs(user_message, prior_messages=retry_messages)
    retry_call_start = time.monotonic()
    try:
        retry_response = sdk_client.messages.create(**retry_kwargs)
    except Exception:
        # First attempt was fine API-wise; second attempt died. We have a
        # report from the first attempt — surface it as a refusal.
        return _downgrade_to_refusal(payload, validation_error_text), UsageStats(
            model=EXTRACTION_MODEL,
            input_tokens=in_t,
            output_tokens=out_t,
            cached_tokens=cache_read,
            cache_write_tokens=cache_write,
            estimated_usd=_estimate_cost(
                in_t,
                out_t,
                cached_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            ),
        )
    retry_generation_seconds = time.monotonic() - retry_call_start

    if time.monotonic() - start > MAX_WALL_CLOCK_SECONDS:
        raise ExtractionTimeoutError(
            f"extraction exceeded {MAX_WALL_CLOCK_SECONDS}s wall-clock budget "
            f"on retry (first call {first_generation_seconds:.1f}s + "
            f"retry call {retry_generation_seconds:.1f}s)",
        )

    retry_payload = _extract_tool_use_payload(retry_response.content)
    retry_stop_reason = _stop_reason(retry_response)
    log.info(
        "extraction_retry_call_complete",
        stop_reason=retry_stop_reason,
        payload_keys=sorted(retry_payload.keys()) if isinstance(retry_payload, dict) else None,
        payload_is_none=retry_payload is None,
        generation_seconds=round(retry_generation_seconds, 2),
    )
    in_t2, out_t2, cache_read2, cache_write2 = _usage_from_response(retry_response)
    # Combine both calls' usage so cost tracking reflects the full work
    # we paid for, including retries.
    combined_in = in_t + in_t2
    combined_out = out_t + out_t2
    combined_read = cache_read + cache_read2
    combined_write = cache_write + cache_write2
    combined_usage = UsageStats(
        model=EXTRACTION_MODEL,
        input_tokens=combined_in,
        output_tokens=combined_out,
        cached_tokens=combined_read,
        cache_write_tokens=combined_write,
        estimated_usd=_estimate_cost(
            combined_in,
            combined_out,
            cached_read_tokens=combined_read,
            cache_write_tokens=combined_write,
        ),
    )

    if retry_payload is None:
        # No tool_use on retry — give up and refuse.
        return _downgrade_to_refusal(payload, validation_error_text), combined_usage

    retry_result_or_text = _result_from_payload(retry_payload, stop_reason=retry_stop_reason)
    if isinstance(retry_result_or_text, ExtractionResult):
        return retry_result_or_text, combined_usage

    # Still failed after retry: fold both error messages into a refusal.
    combined_error = f"{validation_error_text}\nRetry failure:\n{retry_result_or_text}"
    return _downgrade_to_refusal(retry_payload, combined_error), combined_usage


# ---- Payload -> ExtractionResult conversion --------------------------------


def _result_from_payload(
    payload: dict[str, Any],
    *,
    stop_reason: str = "unknown",
) -> ExtractionResult | str:
    """Return an ExtractionResult if the payload validates, else the error.

    Three failure paths produce a fallback:
      1. Payload doesn't contain a valid ExtractionReport.
      2. spec is non-null but fails Phase 1 validate_spec.
      3. spec/report verdict are inconsistent (caught by
         ExtractionResult's model_validator).

    Returning the error text (rather than raising) lets the caller
    decide whether to retry or downgrade.

    `stop_reason` is the Anthropic response's stop_reason field. When
    it is "max_tokens" AND `report` is missing, the most common
    explanation is that generation hit the output-token ceiling
    mid-payload — the SDK parses whatever JSON it could read and the
    later fields drop out. Surfacing that hypothesis in the refusal
    text is far more actionable than the generic "missing field".
    """
    spec_dict = payload.get("spec")
    report_dict = payload.get("report")

    if not isinstance(report_dict, dict):
        if stop_reason == "max_tokens":
            return (
                "tool_use payload missing or malformed `report` field; "
                f"response.stop_reason=max_tokens — generation hit the "
                f"{MAX_OUTPUT_TOKENS}-token output ceiling before the "
                "report field was written. Raise MAX_OUTPUT_TOKENS."
            )
        return f"tool_use payload missing or malformed `report` field (stop_reason={stop_reason})"

    try:
        report = ExtractionReport.model_validate(report_dict)
    except Exception as exc:
        return f"ExtractionReport validation failed: {exc}"

    if spec_dict is None:
        try:
            return ExtractionResult(spec=None, report=report)
        except Exception as exc:
            return f"ExtractionResult validation failed: {exc}"

    if not isinstance(spec_dict, dict):
        return f"tool_use payload `spec` must be object or null, got {type(spec_dict).__name__}"

    try:
        spec, _warnings = validate_spec(spec_dict)
    except StrategySpecValidationErrorGroup as exc:
        # Use the group's stringified errors so the retry message has
        # something the model can actually act on.
        lines = "\n".join(f"- {e}" for e in exc.errors)
        return f"StrategySpec validation failed:\n{lines}"
    except Exception as exc:
        return f"StrategySpec validation raised an unexpected error: {exc}"

    try:
        return ExtractionResult(spec=spec, report=report)
    except Exception as exc:
        return f"ExtractionResult consistency check failed: {exc}"


def _downgrade_to_refusal(
    payload: dict[str, Any],
    error_text: str,
) -> ExtractionResult:
    """Build a NOT_EXTRACTABLE result from a payload whose spec was invalid.

    Re-uses as much of the model's first-attempt report as possible —
    summary, extracted_rules, author_claims, backtestable_parts — but
    overrides the verdict, drops the spec, and appends the validation
    failure to refusal_explanation. This way a partial-quality
    extraction still surfaces useful diagnostic info instead of being
    thrown away.
    """
    report_dict = payload.get("report", {}) if isinstance(payload.get("report"), dict) else {}

    # Start from the model's report if it parses; fall back to a
    # synthesized minimal one if it doesn't.
    try:
        original = ExtractionReport.model_validate(report_dict)
        new_report = original.model_copy(
            update={
                "verdict": ExtractionVerdict.NOT_EXTRACTABLE,
                "refusal_explanation": (
                    "Downgraded after spec validation failed.\n\n"
                    f"Original reasoning:\n{original.reasoning}\n\n"
                    f"Validation error:\n{error_text}"
                ),
            },
        )
    except Exception:
        new_report = ExtractionReport(
            verdict=ExtractionVerdict.NOT_EXTRACTABLE,
            overall_confidence=0.0,
            summary="Extraction failed to produce a schema-valid StrategySpec.",
            extracted_rules=[],
            backtestable_parts=[],
            non_backtestable_parts=[],
            author_claims=[],
            reasoning="The model returned a payload but the spec did not validate.",
            refusal_explanation=(
                f"Extraction downgraded due to schema validation failure:\n{error_text}"
            ),
        )
    return ExtractionResult(spec=None, report=new_report)


__all__ = [
    "EXTRACTION_MODEL",
    "MAX_EXTRACTION_USD",
    "ExtractionError",
    "ExtractionFailedError",
    "ExtractionResult",
    "ExtractionTimeoutError",
    "ExtractionTooExpensiveError",
    "StrategySpec",  # re-export so callers don't need a second import
    "UsageStats",
    "extract_strategy",
]
