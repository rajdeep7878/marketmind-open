"""Unit tests for the LLM extraction service.

The Anthropic SDK is mocked entirely — these tests must not touch the
real API. One opt-in integration test in the same file makes a real
call against a tiny synthetic transcript (skipped by default).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from marketmind_shared.schemas import (
    ExtractionResult,
    ExtractionVerdict,
    Transcript,
    TranscriptSegment,
)
from marketmind_shared.schemas.content import ExtractionInput
from marketmind_workers.services import extract as extract_mod
from marketmind_workers.services.extract import (
    ExtractionFailedError,
    ExtractionTooExpensiveError,
    UsageStats,
    extract_strategy,
)

# ---- Test doubles -----------------------------------------------------------


class _ToolUseBlock:
    """Duck-typed stand-in for an Anthropic tool_use content block."""

    type = "tool_use"

    def __init__(self, payload: dict[str, Any], block_id: str = "tu_test") -> None:
        self.name = "submit_extraction"
        self.id = block_id
        self.input = payload


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    def __init__(
        self,
        *,
        input_tokens: int = 1000,
        output_tokens: int = 500,
        cache_read: int = 0,
        cache_create: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = cache_create


class _FakeResponse:
    def __init__(
        self,
        content: list[Any],
        *,
        usage: _Usage | None = None,
        stop_reason: str = "end_turn",
    ) -> None:
        self.content = content
        self.usage = usage if usage is not None else _Usage()
        self.stop_reason = stop_reason


class _Messages:
    def __init__(self, responses: list[Any] | list[Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAnthropic.messages.create called more times than responses")
        next_item = self._responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class _FakeAnthropic:
    def __init__(self, responses: list[Any] | list[Exception]) -> None:
        self.messages = _Messages(responses)


# ---- Fixture data -----------------------------------------------------------


def _make_transcript(text: str = "hello strategy world") -> Transcript:
    # The segment text is bounded to 10k chars by the schema; the
    # transcript full_text is not. Keep the segment short so we can
    # use this factory with arbitrarily long full_text strings.
    return Transcript(
        language="en",
        full_text=text,
        segments=[
            TranscriptSegment(
                start_seconds=0.0,
                end_seconds=1.0,
                text="placeholder segment",
            ),
        ],
        duration_seconds=1.0,
        model_name="small",
    )


def _make_source() -> ExtractionInput:
    return ExtractionInput(
        source_url="https://example.com/video",
        source_type="youtube",
        transcript=_make_transcript(),
    )


def _golden_cross_spec_dict() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "name": "Golden Cross",
        "description": "",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "Binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "crossover",
                "series": {
                    "kind": "indicator",
                    "name": "sma",
                    "params": {"period": 50},
                    "source": "close",
                },
                "threshold": {
                    "kind": "indicator",
                    "name": "sma",
                    "params": {"period": 200},
                    "source": "close",
                },
                "direction": "above",
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {
                    "type": "condition",
                    "condition": {
                        "type": "crossover",
                        "series": {
                            "kind": "indicator",
                            "name": "sma",
                            "params": {"period": 50},
                            "source": "close",
                        },
                        "threshold": {
                            "kind": "indicator",
                            "name": "sma",
                            "params": {"period": 200},
                            "source": "close",
                        },
                        "direction": "below",
                    },
                },
            ],
        },
    }


def _fully_extractable_payload() -> dict[str, Any]:
    return {
        "spec": _golden_cross_spec_dict(),
        "report": {
            "verdict": "fully_extractable",
            "overall_confidence": 0.85,
            "summary": "Golden cross strategy on BTC/USDT, 4h.",
            "extracted_rules": [
                {
                    "field": "entry",
                    "value_description": "50 SMA crosses above 200 SMA",
                    "extractable": True,
                    "confidence": 0.95,
                    "quote": "When the 50 crosses above the 200",
                },
            ],
            "backtestable_parts": ["entry", "exit"],
            "non_backtestable_parts": [],
            "author_claims": [],
            "reasoning": "All rules numerically defined; no discretion required.",
            "refusal_explanation": None,
        },
    }


def _not_extractable_payload() -> dict[str, Any]:
    return {
        "spec": None,
        "report": {
            "verdict": "not_extractable",
            "overall_confidence": 0.05,
            "summary": "Discretionary support/resistance breakouts.",
            "extracted_rules": [],
            "backtestable_parts": [],
            "non_backtestable_parts": ["entry depends on hand-drawn levels"],
            "author_claims": [],
            "reasoning": "The trader draws S/R by eye; no mechanical rule provided.",
            "refusal_explanation": "Subjective levels — cannot be backtested.",
        },
    }


def _invalid_spec_payload() -> dict[str, Any]:
    """A payload whose spec violates a StrategySpec cross-cutting rule.

    Uses r_multiple TP without a stop loss — Phase 1 rejects this.
    """
    bad = _golden_cross_spec_dict()
    bad["exit"]["exits"].append(
        {
            "type": "take_profit",
            "method": {"kind": "r_multiple", "r_multiple": 2.0},
        },
    )
    return {
        "spec": bad,
        "report": _fully_extractable_payload()["report"],
    }


# ---- Happy paths -----------------------------------------------------------


def test_extract_happy_path_returns_validated_spec() -> None:
    fake = _FakeAnthropic([_FakeResponse([_ToolUseBlock(_fully_extractable_payload())])])
    result, usage = extract_strategy(_make_transcript(), _make_source(), client=fake)

    assert isinstance(result, ExtractionResult)
    assert result.spec is not None
    assert result.report.verdict is ExtractionVerdict.FULLY_EXTRACTABLE
    assert usage.model == "claude-sonnet-4-6"
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 500
    assert usage.estimated_usd > 0


def test_extract_refused_path_returns_null_spec() -> None:
    fake = _FakeAnthropic([_FakeResponse([_ToolUseBlock(_not_extractable_payload())])])
    result, _usage = extract_strategy(_make_transcript(), _make_source(), client=fake)
    assert result.spec is None
    assert result.report.verdict is ExtractionVerdict.NOT_EXTRACTABLE


# ---- Validation failure paths ----------------------------------------------


def test_extract_validation_failure_retries_and_succeeds() -> None:
    fake = _FakeAnthropic(
        [
            _FakeResponse([_ToolUseBlock(_invalid_spec_payload(), block_id="first")]),
            _FakeResponse([_ToolUseBlock(_fully_extractable_payload(), block_id="second")]),
        ],
    )
    result, usage = extract_strategy(_make_transcript(), _make_source(), client=fake)

    assert result.spec is not None
    assert result.report.verdict is ExtractionVerdict.FULLY_EXTRACTABLE
    assert len(fake.messages.calls) == 2
    # Combined usage from both calls
    assert usage.input_tokens == 2000
    assert usage.output_tokens == 1000


def test_extract_validation_failure_twice_downgrades_to_refusal() -> None:
    fake = _FakeAnthropic(
        [
            _FakeResponse([_ToolUseBlock(_invalid_spec_payload())]),
            _FakeResponse([_ToolUseBlock(_invalid_spec_payload())]),
        ],
    )
    result, _usage = extract_strategy(_make_transcript(), _make_source(), client=fake)

    assert result.spec is None
    assert result.report.verdict is ExtractionVerdict.NOT_EXTRACTABLE
    assert "Downgraded after spec validation failed" in (result.report.refusal_explanation or "")


# ---- API failure paths -----------------------------------------------------


def test_extract_api_failure_first_call_raises() -> None:
    fake = _FakeAnthropic([RuntimeError("anthropic 503")])
    with pytest.raises(ExtractionFailedError, match="anthropic 503"):
        extract_strategy(_make_transcript(), _make_source(), client=fake)


def test_extract_no_tool_use_block_raises() -> None:
    fake = _FakeAnthropic([_FakeResponse([_TextBlock("oops I forgot the tool")])])
    with pytest.raises(ExtractionFailedError, match="no submit_extraction tool_use"):
        extract_strategy(_make_transcript(), _make_source(), client=fake)


def test_extract_retry_api_failure_returns_downgraded_refusal() -> None:
    fake = _FakeAnthropic(
        [
            _FakeResponse([_ToolUseBlock(_invalid_spec_payload())]),
            RuntimeError("anthropic timeout"),
        ],
    )
    # Retry path: first attempt failed validation, retry-API died.
    # Service should downgrade rather than re-raise.
    result, _usage = extract_strategy(_make_transcript(), _make_source(), client=fake)
    assert result.spec is None
    assert result.report.verdict is ExtractionVerdict.NOT_EXTRACTABLE


# ---- Truncation diagnostics (stop_reason=max_tokens) -----------------------


def test_result_from_payload_missing_report_with_max_tokens_returns_truncation_text() -> None:
    """When the model hits the output-token ceiling before writing
    `report`, the parsed payload arrives with only `spec`. The
    downgrade refusal text must say "stop_reason=max_tokens" so the
    operator can act (raise MAX_OUTPUT_TOKENS) without re-running the
    failure to confirm. Captured for the 2026-05-19 max_tokens
    incident — see docs/operations/extraction-stop-reason-max-tokens.md.
    """
    partial_payload = {"spec": _golden_cross_spec_dict()}
    result_or_text = extract_mod._result_from_payload(
        partial_payload,
        stop_reason="max_tokens",
    )
    assert isinstance(result_or_text, str)
    assert "stop_reason=max_tokens" in result_or_text
    assert "Raise MAX_OUTPUT_TOKENS" in result_or_text


def test_result_from_payload_missing_report_with_other_stop_reason_uses_generic_text() -> None:
    """Stop reasons other than max_tokens (e.g. end_turn, refusal,
    stop_sequence) get the generic error text with stop_reason tagged
    on. The hypothesis-specific 'raise MAX_OUTPUT_TOKENS' guidance
    only fires for the truncation case so it doesn't mislead in other
    failure modes.
    """
    partial_payload = {"spec": _golden_cross_spec_dict()}
    result_or_text = extract_mod._result_from_payload(
        partial_payload,
        stop_reason="end_turn",
    )
    assert isinstance(result_or_text, str)
    assert "stop_reason=end_turn" in result_or_text
    assert "Raise MAX_OUTPUT_TOKENS" not in result_or_text


def test_extract_first_call_overruns_budget_raises_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK's first call takes longer than MAX_WALL_CLOCK_SECONDS,
    the deadline guard fires AFTER the call returns (the guard is a
    post-call check, not a hard timeout — we don't kill mid-stream).
    The error message includes the actual generation_seconds so an
    operator can tell if the call genuinely stalled or just ran long.
    Captured for the 2026-05-19 wall-clock-budget incident — see
    docs/operations/extraction-wall-clock-budget.md.
    """
    import time as time_mod

    # Tighten the budget to something we can blow through in milliseconds.
    monkeypatch.setattr(extract_mod, "MAX_WALL_CLOCK_SECONDS", 0.01)

    class _SlowMessages:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> _FakeResponse:
            self.calls.append(kwargs)
            time_mod.sleep(0.05)  # > MAX_WALL_CLOCK_SECONDS
            return _FakeResponse(
                [_ToolUseBlock(_fully_extractable_payload())],
                stop_reason="tool_use",
            )

    class _SlowAnthropic:
        def __init__(self) -> None:
            self.messages = _SlowMessages()

    fake = _SlowAnthropic()
    with pytest.raises(extract_mod.ExtractionTimeoutError, match="wall-clock budget"):
        extract_strategy(_make_transcript(), _make_source(), client=fake)


def test_extract_max_tokens_truncation_downgrades_with_truncation_refusal() -> None:
    """End-to-end wiring check: the SDK returns a max_tokens response
    whose tool_use input has only `spec`, the retry SDK call dies, and
    the downgraded refusal carries the truncation-specific text in
    refusal_explanation.
    """
    fake = _FakeAnthropic(
        [
            _FakeResponse(
                [_ToolUseBlock({"spec": _golden_cross_spec_dict()})],
                stop_reason="max_tokens",
            ),
            # Retry dies — mirrors the production failure mode where
            # the half-written first response makes the replay invalid.
            RuntimeError("retry api error"),
        ],
    )
    result, _usage = extract_strategy(_make_transcript(), _make_source(), client=fake)
    assert result.spec is None
    assert result.report.verdict is ExtractionVerdict.NOT_EXTRACTABLE
    refusal = result.report.refusal_explanation or ""
    assert "stop_reason=max_tokens" in refusal
    assert "Raise MAX_OUTPUT_TOKENS" in refusal


# ---- Pre-flight guards -----------------------------------------------------


def test_extract_rejects_pathologically_long_transcript() -> None:
    # 5 million chars -> ~1.25M tokens of input; well above the $0.50 ceiling.
    huge = _make_transcript("x" * 5_000_000)
    src = ExtractionInput(source_url="", source_type="raw_text", transcript=huge)
    with pytest.raises(ExtractionTooExpensiveError):
        extract_strategy(huge, src, client=_FakeAnthropic([]))


# ---- Cost math -------------------------------------------------------------


def test_estimate_cost_no_cache() -> None:
    cost = extract_mod._estimate_cost(input_tokens=100_000, output_tokens=2_000)
    expected = 100_000 / 1_000_000 * 3.0 + 2_000 / 1_000_000 * 15.0
    assert cost == pytest.approx(expected, rel=1e-9)


def test_estimate_cost_with_cache_hit_is_cheaper_than_without() -> None:
    # Anthropic reports `input_tokens` as the non-cached portion only;
    # `cache_read_input_tokens` is reported separately. So a 20k-token
    # call where 18k came from cache shows up as (input=2000, read=18000).
    # Compare that to the same 20k-token call with no cache (input=20000).
    cold = extract_mod._estimate_cost(input_tokens=20_000, output_tokens=1_000)
    warm = extract_mod._estimate_cost(
        input_tokens=2_000,
        output_tokens=1_000,
        cached_read_tokens=18_000,
    )
    assert warm < cold
    # Sanity: the cache-read rate is ~10x cheaper than the input rate,
    # so the warm call's input portion should be roughly 1/3 of the cold.
    assert warm < cold * 0.5


def test_usage_stats_dataclass_is_frozen() -> None:
    u = UsageStats(
        model="claude-sonnet-4-6",
        input_tokens=1,
        output_tokens=1,
        cached_tokens=0,
        cache_write_tokens=0,
        estimated_usd=0.0,
    )
    with pytest.raises((AttributeError, TypeError)):
        u.model = "other"  # type: ignore[misc]


# ---- Prompt-caching shape (no API call) ------------------------------------


def test_request_kwargs_apply_cache_control() -> None:
    from marketmind_workers.services.extraction_prompt import (
        build_submit_extraction_tool,
        system_prompt_blocks,
    )

    tool = build_submit_extraction_tool()
    assert tool["cache_control"] == {"type": "ephemeral"}

    blocks = system_prompt_blocks()
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


# ---- Integration: real API call (opt-in) -----------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_extract_real_api_smoke() -> None:
    """One real-API smoke test with a deliberately-trivial transcript.

    Excluded from CI. The transcript is a one-line non-strategy, so we
    expect a `not_a_strategy` verdict and a tiny (~$0.05 worst case)
    bill. Asserts on shape only — not on the specific verdict text —
    so this stays stable against prompt iterations.
    """
    transcript = _make_transcript(
        "Today I'm going to share my thoughts on the weather and how nice it is outside.",
    )
    source = ExtractionInput(
        source_url="https://example.com/weather",
        source_type="article",
        transcript=transcript,
    )
    # No client argument — use the real SDK against the live API.
    result, usage = extract_strategy(transcript, source)
    assert isinstance(result, ExtractionResult)
    # The transcript is non-strategy, so the model should refuse.
    assert result.report.verdict in {
        ExtractionVerdict.NOT_A_STRATEGY,
        ExtractionVerdict.NOT_EXTRACTABLE,
    }
    assert result.spec is None
    assert usage.estimated_usd < 0.50


# Suppress: imported for re-export-side-effects check
_ = Path
