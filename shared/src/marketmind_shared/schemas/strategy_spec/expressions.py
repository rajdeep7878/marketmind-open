"""Expression union: price / constant / indicator / lagged / scaled.

Recursive: lagged and scaled wrap another expression. Pydantic v2 handles
this via forward refs + model_rebuild() at module load.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.strategy_spec.indicators import IndicatorExpr

# `factor` non-zero + bounded; the doc-spec calls factor=1.0 redundant but valid.
_SCALED_FACTOR_MIN = -1000.0
_SCALED_FACTOR_MAX = 1000.0


class PriceExpr(_StrictModel):
    kind: Literal["price"] = "price"
    field: Literal["open", "high", "low", "close", "volume"]


class ConstantExpr(_StrictModel):
    kind: Literal["constant"] = "constant"
    value: float


class LaggedExpr(_StrictModel):
    kind: Literal["lagged"] = "lagged"
    expression: Expression
    # bars_ago >= 0: the spec's "no look-ahead" rule. 0 collapses to the
    # current bar's value but is permitted (lets extraction be uniform).
    bars_ago: int = Field(ge=0, le=10_000)


class ScaledExpr(_StrictModel):
    kind: Literal["scaled"] = "scaled"
    expression: Expression
    factor: float

    @model_validator(mode="after")
    def _validate_factor(self) -> Self:
        if self.factor == 0:
            raise PydanticCustomError(
                "scaled_factor_zero",
                "scaled.factor must be non-zero",
            )
        if not (_SCALED_FACTOR_MIN <= self.factor <= _SCALED_FACTOR_MAX):
            raise PydanticCustomError(
                "scaled_factor_out_of_bounds",
                "scaled.factor must be in [{min}, {max}], got {value}",
                {
                    "min": _SCALED_FACTOR_MIN,
                    "max": _SCALED_FACTOR_MAX,
                    "value": self.factor,
                },
            )
        return self


class PercentileExpr(_StrictModel):
    """v1.2 wrapper expression: rolling empirical percentile of an inner
    expression at the current bar.

    At each bar, evaluates to the rank-as-fraction (0..1) of the most
    recent value within the trailing ``window`` of values produced by
    ``expression``. Useful for regime detection where the threshold
    should be expressed in distributional terms rather than fixed
    numbers — e.g. "ATR is in the top 30% of its 168-hour distribution".

    NaN convention: the first ``window - 1`` bars produce NaN
    (insufficient history); strict ``min_periods=window`` matches
    pd.Series.rolling(window).rank(pct=True) semantics. Comparisons
    against NaN evaluate to False, so a strategy using a percentile
    will simply not fire during its warmup window.

    Implementation: pure rolling reduction; not stateful in the v2
    Tier-2/Tier-3 sense. Evaluated identically by both the vbt
    translator and the iterative engine via the shared
    ``_eval_expression`` dispatcher — bit-identity by construction.

    See docs/design/v1.2-schema-additions.md §4 v1.2.A.
    """

    kind: Literal["percentile"] = "percentile"
    expression: Expression = Field(
        description="The inner expression whose rolling percentile is computed.",
    )
    window: int = Field(
        ge=10,
        le=10_000,
        description=(
            "Trailing bar count for the rolling distribution. Lower bound "
            "10 keeps the percentile statistically meaningful (a window of "
            "5 has very high variance per bar). Upper bound 10_000 matches "
            "LaggedExpr.bars_ago."
        ),
    )


class RatchetExpr(_StrictModel):
    """v2.0 stateful expression: a value that only moves favorably.

    At each bar this evaluates to the running max (``extremum="max"``)
    or running min (``extremum="min"``) of ``source`` since the last
    reset. It is the general primitive of which a trailing stop is one
    special case — compose it into a `compare`/`crossover` like any
    other expression.

    ``reset="never"`` runs the running extremum over the whole series
    (Tier 2 — a clean numba scan). ``reset="per_trade"`` resets the
    extremum at each position entry (Tier 3 — depends on trade
    boundaries, so it is evaluated only by the custom backtest path).
    See docs/design/v2-phase-a-stateful-conditions.md section 1.1.
    """

    kind: Literal["ratchet"] = "ratchet"
    source: Expression = Field(
        description="The expression whose running extremum is tracked.",
    )
    extremum: Literal["max", "min"] = Field(
        description=(
            "'max' tracks the running maximum (ratchets up); 'min' tracks "
            "the running minimum (ratchets down)."
        ),
    )
    reset: Literal["never", "per_trade"] = Field(
        default="per_trade",
        description=(
            "'never': the running extremum spans the whole series. "
            "'per_trade': it resets at each position entry — use this for "
            "trailing stops."
        ),
    )

    @model_validator(mode="after")
    def _no_nested_ratchet(self) -> Self:
        # Nested ratchets have undefined reset-interaction semantics in
        # v2.0 — a ratchet of a ratchet is rejected at the schema boundary.
        if _expression_contains_ratchet(self.source):
            raise PydanticCustomError(
                "ratchet_nested_unsupported",
                "ratchet.source must not contain another ratchet "
                "(nested ratchet semantics are undefined in v2.0)",
            )
        return self


# The discriminated union. Each variant has a unique `kind` literal so
# Pydantic can route incoming JSON to the right model deterministically.
Expression = Annotated[
    PriceExpr | ConstantExpr | IndicatorExpr | LaggedExpr | ScaledExpr | RatchetExpr | PercentileExpr,
    Field(discriminator="kind"),
]


def _expression_contains_ratchet(expr: Expression) -> bool:
    """True if ``expr`` is, or transitively wraps, a RatchetExpr."""
    if isinstance(expr, RatchetExpr):
        return True
    if isinstance(expr, (LaggedExpr, ScaledExpr, PercentileExpr)):
        return _expression_contains_ratchet(expr.expression)
    return False


# Resolve the forward references used inside Lagged/Scaled/Ratchet/Percentile.
LaggedExpr.model_rebuild()
ScaledExpr.model_rebuild()
RatchetExpr.model_rebuild()
PercentileExpr.model_rebuild()


__all__ = [
    "ConstantExpr",
    "Expression",
    "IndicatorExpr",
    "LaggedExpr",
    "PercentileExpr",
    "PriceExpr",
    "RatchetExpr",
    "ScaledExpr",
]
