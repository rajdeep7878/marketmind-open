"""A.2 tests — the extraction subsystem's v2.0 stateful support.

Three things are checked, all without a real API call:
  1. EXTRACTION_SYSTEM_PROMPT teaches the three new condition types.
  2. The generated `submit_extraction` tool schema carries the new
     types, with model- and field-level descriptions.
  3. A mocked extraction round-trips a stateful (schema_version 2.0)
     spec end-to-end through `extract_strategy`.

Real-API extraction quality is deferred to the A.6 integration tests.
"""

from __future__ import annotations

from typing import Any

from marketmind_shared.schemas import (
    ExtractionResult,
    ExtractionVerdict,
    Transcript,
    TranscriptSegment,
)
from marketmind_shared.schemas.content import ExtractionInput
from marketmind_workers.services.extract import extract_strategy
from marketmind_workers.services.extraction_prompt import (
    EXTRACTION_SYSTEM_PROMPT,
    build_submit_extraction_tool,
)

# ---- minimal Anthropic test doubles (mirrors test_extract.py) --------------


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.name = "submit_extraction"
        self.id = "tu_stateful"
        self.input = payload


class _Usage:
    def __init__(self) -> None:
        self.input_tokens = 1000
        self.output_tokens = 500
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _FakeResponse:
    def __init__(self, content: list[Any]) -> None:
        self.content = content
        self.usage = _Usage()
        self.stop_reason = "tool_use"


class _Messages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _Messages(responses)


# ---- payload helpers -------------------------------------------------------


def _make_transcript(text: str) -> Transcript:
    return Transcript(
        language="en",
        full_text=text,
        segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.0, text="seg")],
        duration_seconds=1.0,
        model_name="small",
    )


def _make_source() -> ExtractionInput:
    return ExtractionInput(
        source_url="https://example.com/v",
        source_type="youtube",
        transcript=_make_transcript("stateful strategy"),
    )


def _ema(period: int) -> dict[str, Any]:
    return {"kind": "indicator", "name": "ema", "params": {"period": period}}


def _report(verdict: str = "fully_extractable") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "overall_confidence": 0.85,
        "summary": "A stateful strategy.",
        "extracted_rules": [],
        "backtestable_parts": ["entry", "exit"],
        "non_backtestable_parts": [],
        "author_claims": [],
        "reasoning": "Mechanical, path-dependent rules.",
        "refusal_explanation": None,
    }


def _regime_spec_dict() -> dict[str, Any]:
    """A schema_version 2.0 spec built around a regime_state condition."""
    return {
        "schema_version": "2.0",
        "name": "Regime Trend Follower",
        "description": "",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "and",
                "conditions": [
                    {
                        "type": "regime_state",
                        "initial": False,
                        "enter_when": {
                            "type": "crossover",
                            "series": {"kind": "price", "field": "close"},
                            "threshold": _ema(200),
                            "direction": "above",
                        },
                        "exit_when": {
                            "type": "crossover",
                            "series": {"kind": "price", "field": "close"},
                            "threshold": _ema(200),
                            "direction": "below",
                        },
                    },
                    {
                        "type": "crossover",
                        "series": _ema(20),
                        "threshold": _ema(50),
                        "direction": "above",
                    },
                ],
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "atr_multiple", "atr_period": 14, "mult": 2.0}},
            ],
        },
    }


def _prior_trade_spec_dict() -> dict[str, Any]:
    """A schema_version 2.0 spec using a prior_trade (skip-after-winner)."""
    return {
        "schema_version": "2.0",
        "name": "Skip After Winner Breakout",
        "description": "",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "and",
                "conditions": [
                    {
                        "type": "crossover",
                        "series": _ema(20),
                        "threshold": _ema(50),
                        "direction": "above",
                    },
                    {
                        "type": "not",
                        "condition": {"type": "prior_trade", "predicate": "last_won", "n": 1},
                    },
                ],
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}],
        },
    }


def _prior_signal_spec_dict() -> dict[str, Any]:
    """A schema_version 2.0 spec using prior_signal (Turtle System 1)."""
    return {
        "schema_version": "2.0",
        "name": "Turtle System 1 Breakout",
        "description": "",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "and",
                "conditions": [
                    {
                        "type": "crossover",
                        "series": _ema(20),
                        "threshold": _ema(50),
                        "direction": "above",
                    },
                    {
                        "type": "not",
                        "condition": {
                            "type": "prior_signal",
                            "predicate": "last_would_have_won",
                        },
                    },
                ],
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}],
        },
    }


# ---- 1. prompt rendering ---------------------------------------------------


def test_prompt_teaches_stateful_conditions() -> None:
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "STATEFUL CONDITIONS" in prompt
    for token in ("ratchet", "regime_state", "prior_trade", "prior_signal"):
        assert token in prompt, f"prompt never mentions {token}"


def test_prompt_teaches_schema_version_gate() -> None:
    # The model must know that a stateful spec needs schema_version "2.0".
    assert '"2.0"' in EXTRACTION_SYSTEM_PROMPT
    assert "PREFER STATIC" in EXTRACTION_SYSTEM_PROMPT


def test_prompt_contains_worked_examples_per_type() -> None:
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert '"kind": "ratchet"' in prompt
    assert '"type": "regime_state"' in prompt
    assert '"type": "prior_trade"' in prompt
    assert '"type": "prior_signal"' in prompt


def test_prompt_distinguishes_prior_trade_from_prior_signal() -> None:
    # The prompt must teach when to reach for prior_signal vs prior_trade —
    # the boundary that keeps Turtle System 1 from being mis-extracted.
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "Choosing between them" in prompt
    assert "Turtle System 1" in prompt
    # ...and must say plainly that prior_trade cannot express it.
    assert "prior_trade CANNOT express" in prompt


def test_prompt_teaches_regime_state_hysteresis() -> None:
    # 2026-05-21 finding: three v2 extractions fell back to a stateless
    # `compare` proxy on genuine hysteresis regimes because the prompt's
    # only regime_state example was the degenerate same-threshold case.
    # The section must now teach hysteresis explicitly.
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "WHEN TO USE regime_state" in prompt
    assert "hysteresis" in prompt.lower()
    assert "degenerate" in prompt
    # The worked example uses DISTINCT enter / exit bands (a real regime,
    # not a same-threshold one a plain compare could reproduce).
    assert '"component": "upper"' in prompt
    assert '"component": "middle"' in prompt


def test_prompt_teaches_highest_lowest_source() -> None:
    # 2026-05-22 finding: a Donchian extraction was rejected at schema
    # validation because highest/lowest need `source` as a params field
    # (unlike SMA/EMA) — and the prompt had no example of them at all.
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "Highest / Lowest" in prompt
    assert "Donchian" in prompt
    # The params.source convention, stated and worked-exampled.
    assert "params" in prompt
    assert '"name": "highest"' in prompt
    assert '"name": "lowest"' in prompt
    assert '"source": "high"' in prompt
    assert '"source": "low"' in prompt


def test_prompt_clarifies_scaled_factor() -> None:
    # The audit found `scaled` was shown only incidentally inside the
    # ratchet example, never explained — a factor clarification was added.
    assert "multiplies its inner expression by a constant" in EXTRACTION_SYSTEM_PROMPT


def test_prompt_teaches_supertrend() -> None:
    # v1.1 whitelist expansion: Supertrend added with a worked example.
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "### Supertrend" in prompt
    assert '"name": "supertrend"' in prompt
    assert '"component": "direction"' in prompt
    assert '"atr_period"' in prompt
    assert '"multiplier"' in prompt
    # Audit discipline: a self-latching indicator must not be wrapped.
    assert "Do NOT wrap it in regime_state" in prompt


def test_prompt_teaches_adx() -> None:
    # v1.1 batch (2026-05-23): ADX added — single-output scalar.
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "### ADX" in prompt
    assert '"name": "adx"' in prompt
    # The convention should be taught (> 25 trending).
    assert "trending" in prompt.lower()
    # Adjacent-primitive integrity: ADX teaching mentions it is single-output.
    assert "single-output" in prompt


def test_prompt_teaches_keltner() -> None:
    # v1.1 batch (2026-05-23): Keltner Channels — multi-output, mirrors bollinger.
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "### Keltner" in prompt
    assert '"name": "keltner"' in prompt
    assert '"component": "upper"' in prompt
    # Three params worked-exampled.
    assert '"period": 20' in prompt
    assert '"atr_period": 10' in prompt
    assert '"multiplier": 2' in prompt


def test_prompt_teaches_psar() -> None:
    # v1.1 batch (2026-05-23): PSAR — multi-output (value/direction).
    prompt = EXTRACTION_SYSTEM_PROMPT
    assert "### PSAR" in prompt
    assert '"name": "psar"' in prompt
    assert '"step": 0.02' in prompt
    assert '"max_step": 0.2' in prompt
    # Same audit discipline as Supertrend: don't wrap a self-latching indicator.
    assert "do NOT wrap PSAR in regime_state" in prompt


# ---- 2. tool schema generation ---------------------------------------------


def test_tool_schema_includes_stateful_types() -> None:
    tool = build_submit_extraction_tool()
    defs = tool["input_schema"]["$defs"]
    for name in (
        "RatchetExpr",
        "RegimeStateCondition",
        "PriorTradeCondition",
        "PriorSignalCondition",
    ):
        assert name in defs, f"{name} missing from the tool $defs"
        assert defs[name].get("description"), f"{name} has no model description"


def test_tool_schema_stateful_fields_carry_descriptions() -> None:
    defs = build_submit_extraction_tool()["input_schema"]["$defs"]
    assert defs["RatchetExpr"]["properties"]["extremum"].get("description")
    assert defs["RatchetExpr"]["properties"]["reset"].get("description")
    assert defs["RegimeStateCondition"]["properties"]["enter_when"].get("description")
    assert defs["PriorTradeCondition"]["properties"]["predicate"].get("description")
    assert defs["PriorSignalCondition"]["properties"]["predicate"].get("description")


def test_tool_schema_still_cache_controlled() -> None:
    # The new types must not have disturbed the cache_control wiring.
    assert build_submit_extraction_tool()["cache_control"] == {"type": "ephemeral"}


# ---- 3. mocked stateful extraction smoke tests -----------------------------


def test_extraction_roundtrips_a_regime_state_spec() -> None:
    payload = {"spec": _regime_spec_dict(), "report": _report()}
    fake = _FakeAnthropic([_FakeResponse([_ToolUseBlock(payload)])])

    result, usage = extract_strategy(_make_transcript("regime"), _make_source(), client=fake)

    assert isinstance(result, ExtractionResult)
    assert result.spec is not None
    assert result.spec.schema_version == "2.0"
    assert result.report.verdict is ExtractionVerdict.FULLY_EXTRACTABLE
    assert usage.estimated_usd > 0


def test_extraction_roundtrips_a_prior_trade_spec() -> None:
    payload = {"spec": _prior_trade_spec_dict(), "report": _report()}
    fake = _FakeAnthropic([_FakeResponse([_ToolUseBlock(payload)])])

    result, _usage = extract_strategy(_make_transcript("skip"), _make_source(), client=fake)

    assert result.spec is not None
    assert result.spec.schema_version == "2.0"
    assert result.report.verdict is ExtractionVerdict.FULLY_EXTRACTABLE


def test_extraction_roundtrips_a_prior_signal_spec() -> None:
    payload = {"spec": _prior_signal_spec_dict(), "report": _report()}
    fake = _FakeAnthropic([_FakeResponse([_ToolUseBlock(payload)])])

    result, _usage = extract_strategy(_make_transcript("turtle"), _make_source(), client=fake)

    assert result.spec is not None
    assert result.spec.schema_version == "2.0"
    assert result.report.verdict is ExtractionVerdict.FULLY_EXTRACTABLE


def test_extraction_downgrades_stateful_spec_missing_version() -> None:
    # A stateful spec wrongly tagged schema_version 1.0 fails validation;
    # the service retries once then downgrades to a refusal (A.1's
    # stateful_requires_schema_v2 rule firing through the extraction path).
    bad = _regime_spec_dict()
    bad["schema_version"] = "1.0"
    payload = {"spec": bad, "report": _report()}
    fake = _FakeAnthropic(
        [
            _FakeResponse([_ToolUseBlock(payload)]),
            _FakeResponse([_ToolUseBlock(payload)]),
        ],
    )
    result, _usage = extract_strategy(_make_transcript("regime"), _make_source(), client=fake)
    assert result.spec is None
    assert result.report.verdict is ExtractionVerdict.NOT_EXTRACTABLE
