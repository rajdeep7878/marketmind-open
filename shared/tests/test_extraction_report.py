"""Tests for the Phase 2.2 ExtractionReport/ExtractionResult models.

Covers:
  - Construction of each sub-model
  - StrEnum serialization (verdict + claim_type)
  - JSON round-trip
  - The verdict <-> spec consistency rule on ExtractionResult
  - The verdict <-> refusal_explanation consistency rule on ExtractionReport
  - Bound enforcement on confidence and string fields
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from marketmind_shared.schemas import (
    AuthorClaim,
    AuthorClaimType,
    ExtractedRule,
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
)
from pydantic import ValidationError

# ---- minimal-valid factory helpers -----------------------------------------


def _make_extracted_rule(**overrides: Any) -> ExtractedRule:
    base: dict[str, Any] = {
        "field": "entry.indicator",
        "value_description": "50-period SMA on close",
        "extractable": True,
        "confidence": 0.95,
        "quote": "fast MA, 50 period SMA",
    }
    base.update(overrides)
    return ExtractedRule(**base)


def _make_claim(**overrides: Any) -> AuthorClaim:
    base: dict[str, Any] = {
        "claim_type": AuthorClaimType.RETURN,
        "value": "6200%",
        "timeframe": "4h",
        "instrument": "BTC/USDT",
        "period": "2020-2026",
        "quote": "over 6,200% total profit",
    }
    base.update(overrides)
    return AuthorClaim(**base)


def _make_report(**overrides: Any) -> ExtractionReport:
    base: dict[str, Any] = {
        "verdict": ExtractionVerdict.FULLY_EXTRACTABLE,
        "overall_confidence": 0.85,
        "summary": "Golden cross strategy.",
        "extracted_rules": [_make_extracted_rule()],
        "backtestable_parts": ["entry", "exit"],
        "non_backtestable_parts": [],
        "author_claims": [_make_claim()],
        "reasoning": "All numeric, no discretion required.",
        "refusal_explanation": None,
    }
    base.update(overrides)
    return ExtractionReport(**base)


def _minimal_spec_dict() -> dict[str, Any]:
    """A complete StrategySpec dict that validates cleanly."""
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


# ---- ExtractionVerdict (StrEnum) -------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "fully_extractable",
        "partially_extractable",
        "not_extractable",
        "not_a_strategy",
    ],
)
def test_extraction_verdict_round_trip(value: str) -> None:
    v = ExtractionVerdict(value)
    assert str(v) == value
    assert ExtractionVerdict(value) is v


def test_extraction_verdict_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        ExtractionVerdict("maybe_extractable")


# ---- AuthorClaim / AuthorClaimType -----------------------------------------


def test_author_claim_round_trip() -> None:
    claim = _make_claim()
    blob = claim.model_dump_json()
    restored = AuthorClaim.model_validate_json(blob)
    assert restored == claim
    # claim_type must serialize to its string value
    assert json.loads(blob)["claim_type"] == "return"


def test_author_claim_requires_quote() -> None:
    with pytest.raises(ValidationError):
        AuthorClaim(
            claim_type=AuthorClaimType.WIN_RATE,
            value="93%",
            quote="",
        )


@pytest.mark.parametrize(
    "claim_type_value",
    ["return", "drawdown", "win_rate", "trade_count", "sharpe", "other"],
)
def test_author_claim_accepts_each_enum_value(claim_type_value: str) -> None:
    claim = AuthorClaim(
        claim_type=AuthorClaimType(claim_type_value),
        value="x",
        quote="y",
    )
    assert claim.claim_type == AuthorClaimType(claim_type_value)


# ---- ExtractedRule ---------------------------------------------------------


def test_extracted_rule_round_trip() -> None:
    rule = _make_extracted_rule()
    restored = ExtractedRule.model_validate_json(rule.model_dump_json())
    assert restored == rule


def test_extracted_rule_quote_optional() -> None:
    rule = _make_extracted_rule(quote=None)
    assert rule.quote is None


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_extracted_rule_rejects_out_of_range_confidence(bad: float) -> None:
    with pytest.raises(ValidationError):
        _make_extracted_rule(confidence=bad)


# ---- ExtractionReport -----------------------------------------------------


def test_extraction_report_round_trip() -> None:
    report = _make_report()
    blob = report.model_dump_json()
    restored = ExtractionReport.model_validate_json(blob)
    assert restored == report


def test_extraction_report_refusal_requires_explanation() -> None:
    with pytest.raises(ValidationError) as ei:
        _make_report(
            verdict=ExtractionVerdict.NOT_EXTRACTABLE,
            refusal_explanation=None,
        )
    assert any("refusal_explanation_required" in str(e["type"]) for e in ei.value.errors())


def test_extraction_report_not_a_strategy_requires_explanation() -> None:
    with pytest.raises(ValidationError):
        _make_report(
            verdict=ExtractionVerdict.NOT_A_STRATEGY,
            refusal_explanation=None,
        )


def test_extraction_report_extractable_forbids_explanation() -> None:
    with pytest.raises(ValidationError) as ei:
        _make_report(
            verdict=ExtractionVerdict.FULLY_EXTRACTABLE,
            refusal_explanation="should not be here",
        )
    assert any("refusal_explanation_forbidden" in str(e["type"]) for e in ei.value.errors())


def test_extraction_report_accepts_empty_lists() -> None:
    report = _make_report(
        extracted_rules=[],
        backtestable_parts=[],
        non_backtestable_parts=[],
        author_claims=[],
    )
    assert report.extracted_rules == []


@pytest.mark.parametrize("bad", [-0.01, 1.5])
def test_extraction_report_rejects_out_of_range_confidence(bad: float) -> None:
    with pytest.raises(ValidationError):
        _make_report(overall_confidence=bad)


# ---- ExtractionResult -----------------------------------------------------


def test_extraction_result_extractable_requires_spec() -> None:
    with pytest.raises(ValidationError) as ei:
        ExtractionResult(spec=None, report=_make_report())
    assert any("spec_required_for_extractable_verdict" in str(e["type"]) for e in ei.value.errors())


def test_extraction_result_refusal_requires_null_spec() -> None:
    spec_dict = _minimal_spec_dict()
    report = _make_report(
        verdict=ExtractionVerdict.NOT_EXTRACTABLE,
        refusal_explanation="manually drawn levels",
    )
    with pytest.raises(ValidationError) as ei:
        ExtractionResult.model_validate({"spec": spec_dict, "report": report.model_dump()})
    assert any("spec_forbidden_for_refusal_verdict" in str(e["type"]) for e in ei.value.errors())


def test_extraction_result_happy_extractable() -> None:
    result = ExtractionResult.model_validate(
        {
            "spec": _minimal_spec_dict(),
            "report": _make_report().model_dump(),
        },
    )
    assert result.spec is not None
    assert result.report.verdict is ExtractionVerdict.FULLY_EXTRACTABLE


def test_extraction_result_happy_refusal() -> None:
    report = _make_report(
        verdict=ExtractionVerdict.NOT_A_STRATEGY,
        refusal_explanation="market commentary only",
    )
    result = ExtractionResult(spec=None, report=report)
    assert result.spec is None


def test_extraction_result_json_round_trip() -> None:
    result = ExtractionResult.model_validate(
        {
            "spec": _minimal_spec_dict(),
            "report": _make_report().model_dump(),
        },
    )
    blob = result.model_dump_json()
    restored = ExtractionResult.model_validate_json(blob)
    assert restored == result
