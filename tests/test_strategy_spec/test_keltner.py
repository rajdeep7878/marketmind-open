"""Schema validation for the Keltner Channels indicator (v1.1 batch)."""

from __future__ import annotations

from typing import Any

import pytest
from marketmind_shared.schemas.strategy_spec.indicators import (
    INDICATOR_DEFAULTS,
    INDICATOR_RULES,
    IndicatorExpr,
    IndicatorName,
)
from pydantic import ValidationError


def _keltner(**overrides: Any) -> IndicatorExpr:
    payload: dict[str, Any] = {
        "kind": "indicator",
        "name": "keltner",
        "params": {"period": 20, "atr_period": 10, "multiplier": 2.0},
        "component": "upper",
    }
    payload.update(overrides)
    return IndicatorExpr.model_validate(payload)


def test_keltner_is_whitelisted() -> None:
    assert IndicatorName.KELTNER.value == "keltner"
    rule = INDICATOR_RULES[IndicatorName.KELTNER]
    assert rule.components == ("upper", "middle", "lower")
    assert set(rule.numeric) == {"period", "atr_period", "multiplier"}
    assert not rule.source_param
    assert INDICATOR_DEFAULTS[IndicatorName.KELTNER] == {
        "period": 20, "atr_period": 10, "multiplier": 2.0,
    }


def test_keltner_valid_expr() -> None:
    expr = _keltner(component="middle")
    assert expr.name is IndicatorName.KELTNER
    assert expr.component == "middle"
    assert expr.params.period == 20


def test_keltner_requires_component() -> None:
    with pytest.raises(ValidationError, match="component"):
        _keltner(component=None)


def test_keltner_rejects_unknown_component() -> None:
    with pytest.raises(ValidationError, match="component"):
        _keltner(component="midband")


def test_keltner_rejects_out_of_bounds() -> None:
    with pytest.raises(ValidationError, match="between"):
        _keltner(params={"period": 4, "atr_period": 10, "multiplier": 2.0})  # period < 5
    with pytest.raises(ValidationError, match="between"):
        _keltner(params={"period": 20, "atr_period": 10, "multiplier": 0.5})  # mult < 1.0


def test_keltner_requires_all_three_params() -> None:
    with pytest.raises(ValidationError, match="required"):
        _keltner(params={"period": 20, "atr_period": 10})  # multiplier missing
