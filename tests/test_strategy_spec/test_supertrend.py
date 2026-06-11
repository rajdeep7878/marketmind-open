"""Schema validation for the Supertrend indicator (v1.1 whitelist addition).

Numeric-bound enforcement is covered generically by test_bounds.py (the
INDICATOR_RULES matrix auto-extends). These tests pin the Supertrend-
specific behaviour: it is whitelisted, multi-output, and rejects the
shapes a bad extraction would produce.
"""

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


def _supertrend(**overrides: Any) -> IndicatorExpr:
    payload: dict[str, Any] = {
        "kind": "indicator",
        "name": "supertrend",
        "params": {"atr_period": 10, "multiplier": 3.0},
        "component": "value",
    }
    payload.update(overrides)
    return IndicatorExpr.model_validate(payload)


def test_supertrend_is_whitelisted() -> None:
    assert IndicatorName.SUPERTREND.value == "supertrend"
    rule = INDICATOR_RULES[IndicatorName.SUPERTREND]
    assert rule.components == ("value", "direction")
    assert set(rule.numeric) == {"atr_period", "multiplier"}
    assert not rule.source_param
    assert INDICATOR_DEFAULTS[IndicatorName.SUPERTREND] == {
        "atr_period": 10,
        "multiplier": 3.0,
    }


def test_supertrend_valid_expr() -> None:
    expr = _supertrend(component="direction")
    assert expr.name is IndicatorName.SUPERTREND
    assert expr.component == "direction"
    assert expr.params.atr_period == 10
    assert expr.params.multiplier == 3.0


def test_supertrend_requires_a_component() -> None:
    # Multi-output indicator — a spec must select value or direction.
    with pytest.raises(ValidationError, match="component"):
        _supertrend(component=None)


def test_supertrend_rejects_unknown_component() -> None:
    with pytest.raises(ValidationError, match="component"):
        _supertrend(component="trend")


def test_supertrend_rejects_out_of_bounds_params() -> None:
    with pytest.raises(ValidationError, match="between"):
        _supertrend(params={"atr_period": 1, "multiplier": 3.0})  # < 2
    with pytest.raises(ValidationError, match="between"):
        _supertrend(params={"atr_period": 10, "multiplier": 0.5})  # < 1.0


def test_supertrend_requires_both_params() -> None:
    with pytest.raises(ValidationError, match="required"):
        _supertrend(params={"atr_period": 10})  # multiplier missing


def test_supertrend_rejects_foreign_params() -> None:
    # period belongs to other indicators — Supertrend must not accept it.
    with pytest.raises(ValidationError, match="does not accept"):
        _supertrend(params={"atr_period": 10, "multiplier": 3.0, "period": 14})
