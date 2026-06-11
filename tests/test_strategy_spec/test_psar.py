"""Schema validation for the PSAR indicator (v1.1 batch)."""

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


def _psar(**overrides: Any) -> IndicatorExpr:
    payload: dict[str, Any] = {
        "kind": "indicator",
        "name": "psar",
        "params": {"step": 0.02, "max_step": 0.2},
        "component": "value",
    }
    payload.update(overrides)
    return IndicatorExpr.model_validate(payload)


def test_psar_is_whitelisted() -> None:
    assert IndicatorName.PSAR.value == "psar"
    rule = INDICATOR_RULES[IndicatorName.PSAR]
    assert rule.components == ("value", "direction")
    assert set(rule.numeric) == {"step", "max_step"}
    assert not rule.source_param
    assert INDICATOR_DEFAULTS[IndicatorName.PSAR] == {"step": 0.02, "max_step": 0.2}


def test_psar_valid_expr() -> None:
    expr = _psar(component="direction")
    assert expr.name is IndicatorName.PSAR
    assert expr.component == "direction"
    assert expr.params.step == 0.02
    assert expr.params.max_step == 0.2


def test_psar_requires_component() -> None:
    with pytest.raises(ValidationError, match="component"):
        _psar(component=None)


def test_psar_rejects_unknown_component() -> None:
    with pytest.raises(ValidationError, match="component"):
        _psar(component="sar")


def test_psar_rejects_out_of_bounds() -> None:
    with pytest.raises(ValidationError, match="between"):
        _psar(params={"step": 0.005, "max_step": 0.2})  # step < 0.01
    with pytest.raises(ValidationError, match="between"):
        _psar(params={"step": 0.02, "max_step": 2.0})  # max_step > 1.0


def test_psar_requires_both_params() -> None:
    with pytest.raises(ValidationError, match="required"):
        _psar(params={"step": 0.02})  # max_step missing
