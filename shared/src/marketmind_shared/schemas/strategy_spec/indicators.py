"""Indicator metadata, parameter bounds, and the IndicatorExpr model.

The bounds table here is the executable form of the "Indicator Parameter
Bounds" section in docs/strategy-spec.md. Any change to one must be
mirrored in the other.

We use a single IndicatorParams model with all possible parameter fields
as Optional, then a per-indicator validator dispatches to the rules table
to enforce which fields are required, which are forbidden, and the
numeric bounds — emitting custom error codes the test fixtures match on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import _StrictModel


class IndicatorName(StrEnum):
    SMA = "sma"
    EMA = "ema"
    WMA = "wma"
    RSI = "rsi"
    MACD = "macd"
    STOCHASTIC = "stochastic"
    ATR = "atr"
    BOLLINGER = "bollinger"
    STDDEV = "stddev"
    VOLUME_SMA = "volume_sma"
    OBV = "obv"
    VWAP = "vwap"
    HIGHEST = "highest"
    LOWEST = "lowest"
    RETURNS = "returns"
    SUPERTREND = "supertrend"
    ADX = "adx"
    KELTNER = "keltner"
    PSAR = "psar"


@dataclass(frozen=True)
class _Numeric:
    """Numeric parameter constraint: closed interval [min_value, max_value]."""

    min_value: float
    max_value: float


@dataclass(frozen=True)
class _Rule:
    """Per-indicator validation rule.

    `numeric`: parameters that must lie in the given interval if present.
    `non_numeric_required`: parameters that must be present but aren't numeric
        (booleans, enums). They're validated by the IndicatorParams model
        itself; we just need to know they're required.
    `components`: None for scalar series; tuple of allowed component names
        for multi-output indicators.
    `source_param`: parameter name that doubles as the price source for
        indicators like `highest`/`lowest` (vs. the IndicatorExpr-level
        source field used by SMA/EMA/etc.).
    """

    numeric: Mapping[str, _Numeric] = field(default_factory=dict)
    non_numeric_required: tuple[str, ...] = ()
    components: tuple[str, ...] | None = None
    source_param: bool = False


# Mirror of docs/strategy-spec.md "Indicator Parameter Bounds". Change here
# requires a change there (and vice versa). Bounds intentionally generous.
INDICATOR_RULES: Final[Mapping[IndicatorName, _Rule]] = {
    IndicatorName.SMA: _Rule(numeric={"period": _Numeric(2, 500)}),
    IndicatorName.EMA: _Rule(numeric={"period": _Numeric(2, 500)}),
    IndicatorName.WMA: _Rule(numeric={"period": _Numeric(2, 500)}),
    IndicatorName.RSI: _Rule(numeric={"period": _Numeric(2, 100)}),
    IndicatorName.MACD: _Rule(
        numeric={
            "fast": _Numeric(2, 100),
            "slow": _Numeric(3, 200),
            "signal": _Numeric(2, 50),
        },
        components=("line", "signal", "hist"),
    ),
    IndicatorName.STOCHASTIC: _Rule(
        numeric={
            "k": _Numeric(2, 100),
            "d": _Numeric(1, 20),
            "smooth": _Numeric(1, 10),
        },
        components=("k", "d"),
    ),
    IndicatorName.ATR: _Rule(numeric={"period": _Numeric(2, 100)}),
    IndicatorName.BOLLINGER: _Rule(
        numeric={
            "period": _Numeric(5, 200),
            "std_dev": _Numeric(0.5, 5.0),
        },
        components=("upper", "middle", "lower"),
    ),
    IndicatorName.STDDEV: _Rule(numeric={"period": _Numeric(2, 200)}),
    IndicatorName.VOLUME_SMA: _Rule(numeric={"period": _Numeric(2, 200)}),
    IndicatorName.OBV: _Rule(),
    IndicatorName.VWAP: _Rule(non_numeric_required=("session_anchored",)),
    IndicatorName.HIGHEST: _Rule(
        numeric={"period": _Numeric(2, 500)},
        source_param=True,
    ),
    IndicatorName.LOWEST: _Rule(
        numeric={"period": _Numeric(2, 500)},
        source_param=True,
    ),
    IndicatorName.RETURNS: _Rule(numeric={"period": _Numeric(1, 100)}),
    IndicatorName.SUPERTREND: _Rule(
        numeric={
            "atr_period": _Numeric(2, 100),
            "multiplier": _Numeric(1.0, 10.0),
        },
        components=("value", "direction"),
    ),
    IndicatorName.ADX: _Rule(numeric={"period": _Numeric(2, 100)}),
    IndicatorName.KELTNER: _Rule(
        numeric={
            "period": _Numeric(5, 200),
            "atr_period": _Numeric(2, 100),
            "multiplier": _Numeric(1.0, 10.0),
        },
        components=("upper", "middle", "lower"),
    ),
    IndicatorName.PSAR: _Rule(
        numeric={
            "step": _Numeric(0.01, 0.1),
            "max_step": _Numeric(0.1, 1.0),
        },
        components=("value", "direction"),
    ),
}


# Defaults from the bounds table, for documentation/auto-fill purposes.
INDICATOR_DEFAULTS: Final[Mapping[IndicatorName, Mapping[str, float | int | bool]]] = {
    IndicatorName.RSI: {"period": 14},
    IndicatorName.MACD: {"fast": 12, "slow": 26, "signal": 9},
    IndicatorName.STOCHASTIC: {"k": 14, "d": 3, "smooth": 3},
    IndicatorName.ATR: {"period": 14},
    IndicatorName.BOLLINGER: {"period": 20, "std_dev": 2.0},
    IndicatorName.STDDEV: {"period": 20},
    IndicatorName.VOLUME_SMA: {"period": 20},
    IndicatorName.RETURNS: {"period": 1},
    IndicatorName.SUPERTREND: {"atr_period": 10, "multiplier": 3.0},
    IndicatorName.ADX: {"period": 14},
    IndicatorName.KELTNER: {"period": 20, "atr_period": 10, "multiplier": 2.0},
    IndicatorName.PSAR: {"step": 0.02, "max_step": 0.2},
}


_PriceSource = Literal["open", "high", "low", "close"]


class IndicatorParams(_StrictModel):
    """Union of every parameter any whitelisted indicator might take.

    All fields nullable; per-indicator validation in IndicatorExpr enforces
    which subset is actually required/allowed for each indicator. Closed
    set (extra="forbid" inherited) so extraction can't sneak through novel
    parameters that the executor wouldn't know how to interpret.
    """

    period: int | None = None
    fast: int | None = None
    slow: int | None = None
    signal: int | None = None
    k: int | None = None
    d: int | None = None
    smooth: int | None = None
    std_dev: float | None = None
    session_anchored: bool | None = None
    # `source` here is used by highest/lowest only (which series to scan).
    # SMA/EMA/etc. use IndicatorExpr.source instead.
    source: _PriceSource | None = None
    # Supertrend: the ATR lookback period and the band multiplier.
    atr_period: int | None = None
    multiplier: float | None = None
    # PSAR: acceleration factor and its cap.
    step: float | None = None
    max_step: float | None = None


class IndicatorExpr(_StrictModel):
    """An expression that evaluates to a number derived from a whitelisted indicator."""

    kind: Literal["indicator"] = "indicator"
    name: IndicatorName
    params: IndicatorParams = Field(default_factory=IndicatorParams)
    source: _PriceSource = "close"
    component: str | None = None

    @model_validator(mode="after")
    def _validate_against_rules(self) -> Self:
        rule = INDICATOR_RULES[self.name]
        present = self.params.model_dump(exclude_none=True)
        name = self.name.value

        # 1. Numeric bounds first — highest-value check, catches things like
        #    RSI(1) or SMA(1_000_000) from a bad extraction.
        for param_name, value in present.items():
            if param_name in rule.numeric:
                bound = rule.numeric[param_name]
                if not (bound.min_value <= float(value) <= bound.max_value):
                    raise PydanticCustomError(
                        "indicator_param_out_of_bounds",
                        "{name}.{param} must be between {min} and {max}, got {value}",
                        {
                            "name": name,
                            "param": param_name,
                            "min": _render_bound(bound.min_value),
                            "max": _render_bound(bound.max_value),
                            "value": value,
                        },
                    )

        # 2. Required numeric params present.
        for required_param in rule.numeric:
            if required_param not in present:
                raise PydanticCustomError(
                    "indicator_param_missing",
                    "{name}.{param} is required",
                    {"name": name, "param": required_param},
                )

        # 3. Required non-numeric params present.
        for required_param in rule.non_numeric_required:
            if required_param not in present:
                raise PydanticCustomError(
                    "indicator_param_missing",
                    "{name}.{param} is required",
                    {"name": name, "param": required_param},
                )

        # 4. source param: highest/lowest take their source in params.
        #    Other indicators must NOT have params.source set (use IndicatorExpr.source).
        if rule.source_param:
            if "source" not in present:
                raise PydanticCustomError(
                    "indicator_param_missing",
                    "{name}.source is required",
                    {"name": name},
                )
        elif "source" in present:
            raise PydanticCustomError(
                "indicator_param_unknown",
                "{name} does not accept params.source; use the IndicatorExpr.source field instead",
                {"name": name},
            )

        # 5. Forbid params not in the rule for this indicator.
        allowed = (
            set(rule.numeric.keys())
            | set(rule.non_numeric_required)
            | ({"source"} if rule.source_param else set())
        )
        for param_name in present:
            if param_name not in allowed:
                raise PydanticCustomError(
                    "indicator_param_unknown",
                    "{name} does not accept parameter '{param}'",
                    {"name": name, "param": param_name},
                )

        # 6. Component constraints — multi-output indicators must specify one;
        #    scalar indicators must not.
        if rule.components is None:
            if self.component is not None:
                raise PydanticCustomError(
                    "indicator_component_not_supported",
                    "{name} returns a scalar series; component must be omitted",
                    {"name": name},
                )
        else:
            if self.component is None:
                raise PydanticCustomError(
                    "indicator_component_required",
                    "{name} returns multiple outputs; specify one of {components}",
                    {"name": name, "components": list(rule.components)},
                )
            if self.component not in rule.components:
                raise PydanticCustomError(
                    "indicator_component_unknown",
                    "{name}.component must be one of {components}, got {value}",
                    {
                        "name": name,
                        "components": list(rule.components),
                        "value": self.component,
                    },
                )

        # 7. MACD cross-parameter rule: slow EMA period must exceed fast.
        if self.name == IndicatorName.MACD:
            fast = present.get("fast")
            slow = present.get("slow")
            if fast is not None and slow is not None and not (float(slow) > float(fast)):
                raise PydanticCustomError(
                    "macd_slow_must_exceed_fast",
                    "macd.slow ({slow}) must be greater than macd.fast ({fast})",
                    {"slow": slow, "fast": fast},
                )

        return self


def _render_bound(value: float) -> str:
    """Render a bound the way it appears in the spec table."""
    if value == int(value):
        return str(int(value))
    return str(value)


__all__ = [
    "INDICATOR_DEFAULTS",
    "INDICATOR_RULES",
    "IndicatorExpr",
    "IndicatorName",
    "IndicatorParams",
]
