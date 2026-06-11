"""StrategySpec — the top-level spec model.

Cross-cutting validators enforced here (not in any sub-model) because
each of them depends on multiple fields:

- r_multiple TP requires a stop_loss exit
- risk_based sizing requires a stop_loss exit
- filter_timeframe must be strictly higher than primary_timeframe
- v2.0 stateful elements require schema_version "2.0"
- regime_state nesting depth is bounded
- (limit-order rules live on EntryRules; MACD slow>fast lives on IndicatorExpr)

Direction-consistency is intentionally NOT a hard error — see
validator.py for the warning collector.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import (
    Direction,
    Instrument,
    Timeframe,
    _StrictModel,
    timeframe_rank,
)
from marketmind_shared.schemas.strategy_spec.conditions import Condition
from marketmind_shared.schemas.strategy_spec.costs import DEFAULT_COST_MODEL, CostModel
from marketmind_shared.schemas.strategy_spec.entry import EntryRules
from marketmind_shared.schemas.strategy_spec.exit import (
    ConditionExit,
    ExitRules,
    StopLossExit,
    TakeProfitExit,
    TakeProfitRMultiple,
)
from marketmind_shared.schemas.strategy_spec.filters import ConditionFilter, Filter
from marketmind_shared.schemas.strategy_spec.introspection import (
    condition_uses_stateful_v2,
    condition_uses_tier3,
    stateful_nesting_depth,
)
from marketmind_shared.schemas.strategy_spec.legs import SpreadConfig, SpreadLeg
from marketmind_shared.schemas.strategy_spec.metadata import Metadata
from marketmind_shared.schemas.strategy_spec.sizing import (
    DEFAULT_POSITION_SIZING,
    PositionSizing,
    RiskBasedSizing,
)

# Maximum nesting depth of regime_state conditions (a regime whose
# enter/exit trigger itself contains a regime, and so on). Beyond this a
# spec is almost certainly an extraction artifact, not a real strategy —
# see docs/design/v2-phase-a-stateful-conditions.md section 2.2.
_MAX_STATEFUL_NESTING: Final[int] = 4


class StrategySpec(_StrictModel):
    """A validated trading strategy specification.

    schema_version "1.0" is the Phase 1 schema. "2.0" (Phase A) adds the
    stateful condition/expression elements — ratchet, regime_state,
    prior_trade. A spec that uses any v2.0 element must declare
    schema_version "2.0"; a "1.0" spec that uses one is rejected.

    The spec is immutable post-construction (frozen). Cross-cutting rules
    are enforced via `_validate_cross_cutting` below; field-level rules
    are enforced on the sub-models they belong to.
    """

    # Literal anchors the version: any value outside this set is rejected
    # at parse time. "2.0" was added in Phase A; a future "3.0" would
    # again force an explicit migration via a widened literal.
    schema_version: Literal["1.0", "2.0"] = "1.0"
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    instrument: Instrument
    primary_timeframe: Timeframe
    filter_timeframe: Timeframe | None = None
    direction: Direction
    entry: EntryRules
    exit: ExitRules
    position_sizing: PositionSizing = Field(default=DEFAULT_POSITION_SIZING)
    costs: CostModel = Field(default=DEFAULT_COST_MODEL)
    filters: list[Filter] = Field(default_factory=list)
    metadata: Metadata = Field(default_factory=Metadata)
    # Phase E.3 (2026-06-06) — multi-leg / market-neutral spread. ADDITIVE:
    # both default to None, so every single-leg spec (the 7 live strategies +
    # the whole corpus) is byte-identical. When set, the spec is a multi-leg
    # spread strategy: `instrument`/`direction` are leg A, `legs` are the
    # additional legs (leg B = legs[0]), and `spread` defines the spread +
    # mean-reversion signal. Simulated by the dedicated perp-pair engine, NOT
    # the single-leg vbt/iterative path. See schemas/strategy_spec/legs.py.
    legs: list[SpreadLeg] | None = None
    spread: SpreadConfig | None = None

    @model_validator(mode="after")
    def _validate_cross_cutting(self) -> Self:
        # 1. r_multiple TP requires stop_loss
        has_stop = any(isinstance(e, StopLossExit) for e in self.exit.exits)
        has_r_multiple = any(
            isinstance(e, TakeProfitExit) and isinstance(e.method, TakeProfitRMultiple)
            for e in self.exit.exits
        )
        if has_r_multiple and not has_stop:
            raise PydanticCustomError(
                "r_multiple_requires_stop_loss",
                "r_multiple take_profit requires a stop_loss exit "
                "(R is undefined without a stop distance)",
            )

        # 2. risk_based sizing requires stop_loss
        if isinstance(self.position_sizing, RiskBasedSizing) and not has_stop:
            raise PydanticCustomError(
                "risk_based_requires_stop_loss",
                "risk_based sizing requires a stop_loss exit "
                "(position size depends on stop_distance)",
            )

        # 3. filter_timeframe must be strictly higher than primary
        if self.filter_timeframe is not None and timeframe_rank(
            self.filter_timeframe
        ) <= timeframe_rank(self.primary_timeframe):
            raise PydanticCustomError(
                "filter_tf_must_be_higher",
                "filter_timeframe must be higher than primary_timeframe "
                "(got primary={primary}, filter={filter})",
                {
                    "primary": self.primary_timeframe.value,
                    "filter": self.filter_timeframe.value,
                },
            )

        # Every top-level Condition reachable from the spec. Sub-conditions
        # are reached by the introspection tree walks.
        conditions: list[Condition] = [self.entry.condition]
        conditions.extend(
            e.condition for e in self.exit.exits if isinstance(e, ConditionExit)
        )
        conditions.extend(
            f.condition for f in self.filters if isinstance(f, ConditionFilter)
        )

        # 4. v2.0 stateful elements require schema_version "2.0".
        if self.schema_version != "2.0" and any(
            condition_uses_stateful_v2(c) for c in conditions
        ):
            raise PydanticCustomError(
                "stateful_requires_schema_v2",
                "spec uses a v2.0 stateful element (ratchet / regime_state / "
                "prior_trade) but declares schema_version '{version}'; "
                "set schema_version to '2.0'",
                {"version": self.schema_version},
            )

        # 5. Bound pathological nesting of regime_state conditions.
        depth = max((stateful_nesting_depth(c) for c in conditions), default=0)
        if depth > _MAX_STATEFUL_NESTING:
            raise PydanticCustomError(
                "stateful_nesting_too_deep",
                "regime_state nesting depth {depth} exceeds the limit of "
                "{max}; flatten the strategy",
                {"depth": depth, "max": _MAX_STATEFUL_NESTING},
            )

        # 6. Multi-leg / spread consistency (Phase E.3). ADDITIVE: a
        #    single-leg spec (legs is None AND spread is None) skips all of
        #    this and is byte-identical to a pre-E.3 spec.
        self._validate_multi_leg()

        return self

    def _validate_multi_leg(self) -> None:
        """legs/spread must be both-set or both-None; if set, legs are
        non-empty, bounded, and every symbol (including leg A) is distinct."""
        if self.legs is None and self.spread is None:
            return  # single-leg — nothing to check
        if (self.legs is None) != (self.spread is None):
            raise PydanticCustomError(
                "multi_leg_requires_both",
                "a multi-leg spec needs BOTH `legs` and `spread` set (or "
                "NEITHER for a single-leg spec); got legs set: {l}, spread set: {s}",
                {"l": self.legs is not None, "s": self.spread is not None},
            )
        assert self.legs is not None
        if not 1 <= len(self.legs) <= 3:
            raise PydanticCustomError(
                "multi_leg_count",
                "a multi-leg spec needs 1..3 additional legs (leg A is the "
                "primary `instrument`); got {n}",
                {"n": len(self.legs)},
            )
        symbols = [self.instrument.symbol, *(leg.instrument.symbol for leg in self.legs)]
        if len(set(symbols)) != len(symbols):
            raise PydanticCustomError(
                "multi_leg_duplicate_symbol",
                "multi-leg symbols must be distinct (leg A + additional legs); got {syms}",
                {"syms": symbols},
            )


StrategySpec.model_rebuild()


def spec_uses_stateful_v2(spec: StrategySpec) -> bool:
    """True if `spec` uses any v2.0 stateful element — ratchet, regime_state,
    prior_trade, or prior_signal — anywhere in its entry, exit, or filter
    conditions.

    A spec-level wrapper over `condition_uses_stateful_v2`, gathering the
    same top-level conditions `_validate_cross_cutting` checks. The
    overfitting analyses (Phase A.4) use it to select the stateful code
    paths — the continuous-run walk-forward and the re-weighted composite
    score. See docs/design/v2-phase-a-stateful-conditions.md section 5.
    """
    conditions: list[Condition] = [spec.entry.condition]
    conditions.extend(e.condition for e in spec.exit.exits if isinstance(e, ConditionExit))
    conditions.extend(f.condition for f in spec.filters if isinstance(f, ConditionFilter))
    return any(condition_uses_stateful_v2(c) for c in conditions)


def spec_uses_tier3(spec: StrategySpec) -> bool:
    """True if `spec` uses any Tier-3 (outcome-dependent) element — a
    `prior_trade` / `prior_signal` condition, or a `ratchet reset="per_trade"` —
    anywhere in its entry, exit, or filter conditions.

    Tier-3 needs the iterative backtest path; in the live trader its
    execution is Phase A.6, not A.5. See
    docs/design/v2-phase-a-stateful-conditions.md section 6A.
    """
    conditions: list[Condition] = [spec.entry.condition]
    conditions.extend(e.condition for e in spec.exit.exits if isinstance(e, ConditionExit))
    conditions.extend(f.condition for f in spec.filters if isinstance(f, ConditionFilter))
    return any(condition_uses_tier3(c) for c in conditions)


__all__ = ["StrategySpec", "spec_uses_stateful_v2", "spec_uses_tier3"]
