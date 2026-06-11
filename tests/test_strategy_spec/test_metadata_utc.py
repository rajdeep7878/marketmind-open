"""Metadata.extracted_at must be timezone-aware UTC.

Naive datetimes and non-UTC tz-aware datetimes are rejected with error
code `metadata_extracted_at_must_be_utc`. UTC datetimes pass through
and round-trip cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from marketmind_shared.schemas.strategy_spec import (
    StrategySpecValidationErrorGroup,
    validate_spec,
)


def _base_spec_with_extracted_at(extracted_at: str | None) -> dict[str, Any]:
    """Minimal valid spec carrier with Metadata.extracted_at set to the given string."""
    return {
        "schema_version": "1.0",
        "name": "UTC test carrier",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "compare",
                "left": {"kind": "price", "field": "close"},
                "op": ">",
                "right": {"kind": "indicator", "name": "sma", "params": {"period": 20}},
            },
            "order_type": "market",
        },
        "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
        "metadata": {"extracted_at": extracted_at} if extracted_at is not None else {},
    }


def test_timezone_aware_utc_accepts() -> None:
    spec, warnings = validate_spec(_base_spec_with_extracted_at("2026-05-14T12:34:56+00:00"))
    assert warnings == []
    assert spec.metadata.extracted_at is not None
    assert spec.metadata.extracted_at.tzinfo is not None
    # Normalized to UTC tzinfo (timedelta(0) offset).
    assert spec.metadata.extracted_at.utcoffset() == timedelta(0)


def test_timezone_aware_utc_z_suffix_accepts() -> None:
    # "Z" is ISO 8601 shorthand for UTC; Pydantic accepts it.
    spec, _ = validate_spec(_base_spec_with_extracted_at("2026-05-14T12:34:56Z"))
    assert spec.metadata.extracted_at is not None
    assert spec.metadata.extracted_at.utcoffset() == timedelta(0)


def test_naive_datetime_rejects() -> None:
    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(_base_spec_with_extracted_at("2026-05-14T12:34:56"))
    codes = [e.error_code for e in excinfo.value.errors]
    assert "metadata_extracted_at_must_be_utc" in codes, codes
    matching = next(
        e for e in excinfo.value.errors if e.error_code == "metadata_extracted_at_must_be_utc"
    )
    assert "naive datetime" in matching.message


def test_non_utc_timezone_rejects() -> None:
    # +05:30 is India Standard Time — tz-aware but not UTC.
    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(_base_spec_with_extracted_at("2026-05-14T12:34:56+05:30"))
    codes = [e.error_code for e in excinfo.value.errors]
    assert "metadata_extracted_at_must_be_utc" in codes, codes
    matching = next(
        e for e in excinfo.value.errors if e.error_code == "metadata_extracted_at_must_be_utc"
    )
    assert "offset" in matching.message


def test_extracted_at_optional() -> None:
    # No metadata at all — default is None; should pass without warning.
    spec, warnings = validate_spec(_base_spec_with_extracted_at(None))
    assert spec.metadata.extracted_at is None
    assert warnings == []


def test_python_constructed_naive_rejects() -> None:
    # Build via model_validate with a raw Python datetime (no isoformat string).
    data = _base_spec_with_extracted_at(None)
    data["metadata"] = {
        "extracted_at": datetime(2026, 5, 14, 12, 34, 56)  # noqa: DTZ001  (naive is the whole point)
    }
    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(data)
    codes = [e.error_code for e in excinfo.value.errors]
    assert "metadata_extracted_at_must_be_utc" in codes, codes


def test_python_constructed_utc_aware_accepts() -> None:
    data = _base_spec_with_extracted_at(None)
    data["metadata"] = {"extracted_at": datetime(2026, 5, 14, 12, 34, 56, tzinfo=UTC)}
    spec, _ = validate_spec(data)
    assert spec.metadata.extracted_at == datetime(2026, 5, 14, 12, 34, 56, tzinfo=UTC)


def test_python_constructed_non_utc_aware_rejects() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    data = _base_spec_with_extracted_at(None)
    data["metadata"] = {"extracted_at": datetime(2026, 5, 14, 12, 34, 56, tzinfo=ist)}
    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(data)
    codes = [e.error_code for e in excinfo.value.errors]
    assert "metadata_extracted_at_must_be_utc" in codes, codes
