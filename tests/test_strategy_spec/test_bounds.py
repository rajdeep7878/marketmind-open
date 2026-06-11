"""Per-indicator parameter bound enforcement.

For every numeric parameter in INDICATOR_RULES, exercise four points:
- at min: accept
- at max: accept
- one below min: reject with indicator_param_out_of_bounds
- one above max: reject with indicator_param_out_of_bounds

This is the most surface-area-covering test in the suite — it ensures
any future indicator addition cannot silently drop a bound.
"""

from __future__ import annotations

from typing import Any

import pytest
from marketmind_shared.schemas.strategy_spec import (
    StrategySpecValidationErrorGroup,
    validate_spec,
)
from marketmind_shared.schemas.strategy_spec.indicators import (
    INDICATOR_RULES,
    IndicatorName,
)


def _step_below(value: float) -> float | int:
    """One discrete step below `value`. Ints step by 1; floats by min-bound resolution."""
    if isinstance(value, int) or value == int(value):
        return int(value) - 1
    return round(value - 0.1, 2)


def _step_above(value: float) -> float | int:
    if isinstance(value, int) or value == int(value):
        return int(value) + 1
    return round(value + 0.1, 2)


def _wrap_spec(indicator_payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal valid spec that uses the given indicator in a compare.

    Compare(close > <indicator>) is the simplest carrier. Stop is a fixed
    1% so the spec passes top-level validation when the indicator does.
    """
    return {
        "schema_version": "1.0",
        "name": "Bounds test carrier",
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
                "right": indicator_payload,
            },
            "order_type": "market",
        },
        "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.01}}]},
    }


def _build_indicator_payload(
    name: IndicatorName,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Construct an IndicatorExpr JSON with the given param overrides.

    Fills any other required numeric params at the midpoint of their bounds
    so only the parameter under test is at the boundary; fills required
    non-numeric params with sensible defaults.
    """
    rule = INDICATOR_RULES[name]
    params: dict[str, Any] = {}
    for param_name, bound in rule.numeric.items():
        if param_name in overrides:
            params[param_name] = overrides[param_name]
        else:
            # If both bounds are whole numbers we treat the param as int —
            # Pydantic int fields reject non-whole floats like 101.5.
            mid = (bound.min_value + bound.max_value) / 2
            is_int_param = bound.min_value == int(bound.min_value) and bound.max_value == int(
                bound.max_value
            )
            params[param_name] = int(mid) if is_int_param else round(mid, 2)
    for non_numeric in rule.non_numeric_required:
        if non_numeric == "session_anchored":
            params[non_numeric] = True
    if rule.source_param:
        params["source"] = "high" if name == IndicatorName.HIGHEST else "low"

    # MACD: ensure fast < slow even at boundaries.
    if name == IndicatorName.MACD:
        fast = params.get("fast", 12)
        slow = params.get("slow", 26)
        if not (slow > fast):
            # Use defaults that satisfy the constraint when overriding fast/slow.
            if "slow" not in overrides:
                params["slow"] = max(fast + 1, INDICATOR_RULES[name].numeric["slow"].min_value)
            elif "fast" not in overrides:
                params["fast"] = min(slow - 1, INDICATOR_RULES[name].numeric["fast"].max_value)

    payload: dict[str, Any] = {"kind": "indicator", "name": name.value, "params": params}
    if rule.components is not None:
        payload["component"] = rule.components[0]
    return payload


# Build the parameter matrix: (indicator, param_name, bound_min, bound_max) — only numeric params.
_BOUNDS_MATRIX: list[tuple[IndicatorName, str, float, float]] = [
    (name, param, bound.min_value, bound.max_value)
    for name, rule in INDICATOR_RULES.items()
    for param, bound in rule.numeric.items()
]


@pytest.mark.parametrize(
    ("indicator", "param", "bound_min", "bound_max"),
    _BOUNDS_MATRIX,
    ids=[f"{n.value}.{p}" for (n, p, _, _) in _BOUNDS_MATRIX],
)
def test_bound_min_accepts(
    indicator: IndicatorName,
    param: str,
    bound_min: float,
    bound_max: float,
) -> None:
    payload = _build_indicator_payload(indicator, {param: bound_min})
    spec_dict = _wrap_spec(payload)
    validate_spec(spec_dict)


@pytest.mark.parametrize(
    ("indicator", "param", "bound_min", "bound_max"),
    _BOUNDS_MATRIX,
    ids=[f"{n.value}.{p}" for (n, p, _, _) in _BOUNDS_MATRIX],
)
def test_bound_max_accepts(
    indicator: IndicatorName,
    param: str,
    bound_min: float,
    bound_max: float,
) -> None:
    payload = _build_indicator_payload(indicator, {param: bound_max})
    spec_dict = _wrap_spec(payload)
    validate_spec(spec_dict)


@pytest.mark.parametrize(
    ("indicator", "param", "bound_min", "bound_max"),
    _BOUNDS_MATRIX,
    ids=[f"{n.value}.{p}" for (n, p, _, _) in _BOUNDS_MATRIX],
)
def test_bound_below_min_rejects(
    indicator: IndicatorName,
    param: str,
    bound_min: float,
    bound_max: float,
) -> None:
    below = _step_below(bound_min)
    payload = _build_indicator_payload(indicator, {param: below})
    spec_dict = _wrap_spec(payload)
    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(spec_dict)
    # We accept either the bounds-out-of-range error OR the bound's int/float
    # field constraint error (some Field-level constraints catch it first).
    codes = [e.error_code for e in excinfo.value.errors]
    assert any(c == "indicator_param_out_of_bounds" or "greater_than" in c for c in codes), codes


@pytest.mark.parametrize(
    ("indicator", "param", "bound_min", "bound_max"),
    _BOUNDS_MATRIX,
    ids=[f"{n.value}.{p}" for (n, p, _, _) in _BOUNDS_MATRIX],
)
def test_bound_above_max_rejects(
    indicator: IndicatorName,
    param: str,
    bound_min: float,
    bound_max: float,
) -> None:
    above = _step_above(bound_max)
    payload = _build_indicator_payload(indicator, {param: above})
    spec_dict = _wrap_spec(payload)
    with pytest.raises(StrategySpecValidationErrorGroup) as excinfo:
        validate_spec(spec_dict)
    codes = [e.error_code for e in excinfo.value.errors]
    assert any(c == "indicator_param_out_of_bounds" or "less_than" in c for c in codes), codes
