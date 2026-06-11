"""Schema validation for the ADX indicator (v1.1 whitelist addition).

Numeric-bound enforcement is covered generically by test_bounds.py (the
INDICATOR_RULES matrix auto-extends). These tests pin the ADX-specific
behaviour: it is whitelisted, single-output scalar, rejects garbage.
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


def _adx(**overrides: Any) -> IndicatorExpr:
    payload: dict[str, Any] = {
        "kind": "indicator",
        "name": "adx",
        "params": {"period": 14},
    }
    payload.update(overrides)
    return IndicatorExpr.model_validate(payload)


def test_adx_is_whitelisted() -> None:
    assert IndicatorName.ADX.value == "adx"
    rule = INDICATOR_RULES[IndicatorName.ADX]
    assert rule.components is None  # scalar series
    assert set(rule.numeric) == {"period"}
    assert not rule.source_param
    assert INDICATOR_DEFAULTS[IndicatorName.ADX] == {"period": 14}


def test_adx_valid_expr() -> None:
    expr = _adx()
    assert expr.name is IndicatorName.ADX
    assert expr.params.period == 14
    assert expr.component is None  # scalar; component must be omitted


def test_adx_rejects_component() -> None:
    # Scalar indicator — must NOT carry a component.
    with pytest.raises(ValidationError, match="component must be omitted"):
        _adx(component="adx")


def test_adx_rejects_out_of_bounds() -> None:
    with pytest.raises(ValidationError, match="between"):
        _adx(params={"period": 1})  # < 2
    with pytest.raises(ValidationError, match="between"):
        _adx(params={"period": 101})  # > 100


def test_adx_rejects_foreign_params() -> None:
    with pytest.raises(ValidationError, match="does not accept"):
        _adx(params={"period": 14, "multiplier": 3.0})  # multiplier belongs to others
