"""Validation entrypoint with stable error codes and soft-warning collection.

`validate_spec(data)` is the single boundary the rest of the system uses
to turn untyped JSON into a typed StrategySpec. It either returns the
spec plus a list of ExtractionNote warnings (for soft issues like
direction inconsistency), or raises a StrategySpecValidationErrorGroup
with one or more typed StrategySpecValidationErrors.

Why a wrapper rather than letting callers catch Pydantic's ValidationError
directly: Pydantic's error shape is verbose and uses internal slug names
(string_too_short, value_error). Our stable error_codes — set via
PydanticCustomError in validators — are what tests and UI match against.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from marketmind_shared.schemas.strategy_spec.common import Direction
from marketmind_shared.schemas.strategy_spec.conditions import (
    Condition,
    PriorTradeCondition,
)
from marketmind_shared.schemas.strategy_spec.errors import (
    StrategySpecValidationError,
    StrategySpecValidationErrorGroup,
)
from marketmind_shared.schemas.strategy_spec.exit import (
    ConditionExit,
    StopLossExit,
    StopLossFixedPrice,
    StopLossPercent,
    StopLossTrailingPercent,
    TakeProfitExit,
    TakeProfitFixedPrice,
)
from marketmind_shared.schemas.strategy_spec.filters import ConditionFilter
from marketmind_shared.schemas.strategy_spec.introspection import iter_conditions
from marketmind_shared.schemas.strategy_spec.metadata import ExtractionNote
from marketmind_shared.schemas.strategy_spec.spec import StrategySpec


def validate_spec(
    data: Mapping[str, Any],
) -> tuple[StrategySpec, list[ExtractionNote]]:
    """Validate a raw spec dict.

    Returns the parsed StrategySpec and a list of soft-warning ExtractionNotes
    that did NOT block validation (e.g., direction-consistency hints). Hard
    failures raise StrategySpecValidationErrorGroup with typed sub-errors.
    """
    try:
        spec = StrategySpec.model_validate(dict(data))
    except ValidationError as exc:
        errors = _convert_errors(exc)
        raise StrategySpecValidationErrorGroup(errors) from None

    warnings = [
        *_collect_direction_warnings(spec),
        *_collect_prior_trade_warnings(spec),
    ]
    return spec, warnings


def _convert_errors(exc: ValidationError) -> list[StrategySpecValidationError]:
    """Translate Pydantic's error list into our stable shape.

    Pydantic's `type` field is our error_code: for custom validators that
    use PydanticCustomError, it's the slug we chose; for built-in errors
    (e.g., 'string_too_short'), it's Pydantic's own slug. We pass both
    through unchanged so tests can match either.
    """
    out: list[StrategySpecValidationError] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        # Skip the discriminator tag in loc tuples (e.g., 'crossover' inside
        # a Condition union), which appears as a literal string rather than
        # a field name. Keeping these in field_path makes paths noisy and
        # brittle for tests; dropping them yields the user-facing path.
        cleaned: list[str] = []
        for part in loc:
            # Pydantic's loc parts are str (field names + discriminator tags)
            # or int (list indices). We keep discriminator tags in the path —
            # downstream tests use substring matching, which tolerates them.
            if isinstance(part, int):
                cleaned.append(f"[{part}]")
            else:
                cleaned.append(str(part))
        field_path = ".".join(cleaned).replace(".[", "[")
        out.append(
            StrategySpecValidationError(
                error_code=str(err.get("type", "value_error")),
                field_path=field_path,
                message=str(err.get("msg", "")),
            )
        )
    return out


def _collect_direction_warnings(spec: StrategySpec) -> list[ExtractionNote]:
    """Soft direction-consistency checks.

    The spec's validation rule #6 says direction inconsistency is a "soft
    warning, not hard rejection". This implements the catchable cases:

    1. A long strategy with a percent stop_loss where value < 0 (interpreted
       as implying stop ABOVE entry). Same logic inverted for short.
    2. A strategy with both a fixed_price stop_loss and a fixed_price
       take_profit where the relative position is wrong for the direction.
    """
    warnings: list[ExtractionNote] = []

    for idx, exit_cond in enumerate(spec.exit.exits):
        if isinstance(exit_cond, StopLossExit):
            method = exit_cond.method
            # Case 1: percent stop with sign implying wrong direction.
            if isinstance(method, (StopLossPercent, StopLossTrailingPercent)):
                if spec.direction is Direction.LONG and method.value < 0:
                    warnings.append(
                        ExtractionNote(
                            severity="warning",
                            field=f"exit.exits[{idx}].method.value",
                            message=(
                                f"stop_loss percent of {method.value} implies stop ABOVE "
                                "entry for a long strategy; review direction consistency"
                            ),
                            confidence=0.7,
                        )
                    )
                elif (
                    spec.direction is Direction.SHORT
                    and method.value > 0
                    and isinstance(method, StopLossPercent)
                ):
                    # For shorts, a positive percent is the conventional "stop X% above entry";
                    # we don't warn here. Only trailing_percent (which is always positive by
                    # field constraint) is unambiguous for shorts.
                    pass

    # Case 2: fixed_price stop and TP on the wrong side for direction.
    fixed_stop_price: float | None = None
    fixed_tp_price: float | None = None
    for exit_cond in spec.exit.exits:
        if isinstance(exit_cond, StopLossExit) and isinstance(exit_cond.method, StopLossFixedPrice):
            fixed_stop_price = exit_cond.method.price
        elif isinstance(exit_cond, TakeProfitExit) and isinstance(
            exit_cond.method, TakeProfitFixedPrice
        ):
            fixed_tp_price = exit_cond.method.price

    if fixed_stop_price is not None and fixed_tp_price is not None:
        if spec.direction is Direction.LONG and fixed_stop_price > fixed_tp_price:
            warnings.append(
                ExtractionNote(
                    severity="warning",
                    field="exit.exits",
                    message=(
                        f"long strategy with fixed_price stop {fixed_stop_price} > "
                        f"fixed_price take_profit {fixed_tp_price}; review direction consistency"
                    ),
                    confidence=0.8,
                )
            )
        elif spec.direction is Direction.SHORT and fixed_stop_price < fixed_tp_price:
            warnings.append(
                ExtractionNote(
                    severity="warning",
                    field="exit.exits",
                    message=(
                        f"short strategy with fixed_price stop {fixed_stop_price} < "
                        f"fixed_price take_profit {fixed_tp_price}; review direction consistency"
                    ),
                    confidence=0.8,
                )
            )

    return warnings


def _collect_prior_trade_warnings(spec: StrategySpec) -> list[ExtractionNote]:
    """Soft warning: a prior_trade condition with a `last_won`/`last_lost`
    predicate ignores `n` — only the `consecutive_*` predicates use it.
    Surfacing this catches an extraction that set `n` expecting an effect.
    """
    warnings: list[ExtractionNote] = []
    roots: list[Condition] = [spec.entry.condition]
    roots.extend(e.condition for e in spec.exit.exits if isinstance(e, ConditionExit))
    roots.extend(f.condition for f in spec.filters if isinstance(f, ConditionFilter))
    for root in roots:
        for cond in iter_conditions(root):
            if (
                isinstance(cond, PriorTradeCondition)
                and cond.predicate in ("last_won", "last_lost")
                and cond.n != 1
            ):
                warnings.append(
                    ExtractionNote(
                        severity="warning",
                        field="prior_trade.n",
                        message=(
                            f"prior_trade predicate '{cond.predicate}' ignores n; "
                            f"n={cond.n} has no effect (only the consecutive_* "
                            "predicates use n)"
                        ),
                        confidence=0.9,
                    )
                )
    return warnings


__all__ = ["validate_spec"]
