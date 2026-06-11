"""Tree-walk utilities over Condition / Expression trees.

The validator uses these to detect v2.0 (stateful) features, classify a
spec's evaluation tier, and bound nesting depth. The backtest engine
reuses the same walks for tier dispatch — see
docs/design/v2-phase-a-stateful-conditions.md section 2.

These functions operate on Condition and Expression objects only and
never import StrategySpec, which keeps the package import graph acyclic.
"""

from __future__ import annotations

from collections.abc import Iterator

from marketmind_shared.schemas.strategy_spec.conditions import (
    AndCondition,
    CompareCondition,
    Condition,
    CrossoverCondition,
    FallingCondition,
    NotCondition,
    OrCondition,
    RegimeStateCondition,
    RisingCondition,
    WithinLastNBarsCondition,
)
from marketmind_shared.schemas.strategy_spec.expressions import (
    Expression,
    LaggedExpr,
    PercentileExpr,
    RatchetExpr,
    ScaledExpr,
)

# Condition `type` tags introduced in schema v2.0.
V2_CONDITION_TYPES: frozenset[str] = frozenset(
    {"regime_state", "prior_trade", "prior_signal"},
)
# Tier-3 (trade-outcome-dependent) condition `type` tags. These require the
# custom backtest path; vectorbt's from_signals cannot evaluate them.
T3_CONDITION_TYPES: frozenset[str] = frozenset({"prior_trade", "prior_signal"})


def iter_expressions(expr: Expression) -> Iterator[Expression]:
    """Yield `expr` then every nested sub-expression, depth-first."""
    yield expr
    if isinstance(expr, (LaggedExpr, ScaledExpr, PercentileExpr)):
        yield from iter_expressions(expr.expression)
    elif isinstance(expr, RatchetExpr):
        yield from iter_expressions(expr.source)


def iter_conditions(cond: Condition) -> Iterator[Condition]:
    """Yield `cond` then every nested sub-condition, depth-first."""
    yield cond
    if isinstance(cond, (AndCondition, OrCondition)):
        for child in cond.conditions:
            yield from iter_conditions(child)
    elif isinstance(cond, (NotCondition, WithinLastNBarsCondition)):
        yield from iter_conditions(cond.condition)
    elif isinstance(cond, RegimeStateCondition):
        yield from iter_conditions(cond.enter_when)
        yield from iter_conditions(cond.exit_when)


def condition_direct_expressions(cond: Condition) -> Iterator[Expression]:
    """Yield the Expression(s) attached directly to one condition node.

    Compound conditions (and/or/not/within/regime) and leaf conditions
    (candle_pattern/prior_trade) carry no direct expression and yield
    nothing — their expressions live inside sub-conditions.
    """
    if isinstance(cond, CompareCondition):
        yield cond.left
        yield cond.right
    elif isinstance(cond, CrossoverCondition):
        yield cond.series
        yield cond.threshold
    elif isinstance(cond, (RisingCondition, FallingCondition)):
        yield cond.series


def iter_all_expressions(cond: Condition) -> Iterator[Expression]:
    """Yield every Expression anywhere under `cond`, fully expanded
    through nested conditions and nested expressions.
    """
    for sub in iter_conditions(cond):
        for direct in condition_direct_expressions(sub):
            yield from iter_expressions(direct)


def condition_uses_stateful_v2(cond: Condition) -> bool:
    """True if `cond` (or anything nested in it) uses a v2.0 stateful
    element — a regime_state / prior_trade condition or a ratchet
    expression.
    """
    if any(sub.type in V2_CONDITION_TYPES for sub in iter_conditions(cond)):
        return True
    return any(isinstance(e, RatchetExpr) for e in iter_all_expressions(cond))


def condition_uses_tier3(cond: Condition) -> bool:
    """True if `cond` needs the Tier-3 custom backtest path: a prior_trade
    condition, or a ratchet with reset='per_trade'. Both are
    trade-outcome / trade-boundary dependent and cannot be precomputed as
    a vectorbt signal series.
    """
    if any(sub.type in T3_CONDITION_TYPES for sub in iter_conditions(cond)):
        return True
    return any(
        isinstance(e, RatchetExpr) and e.reset == "per_trade"
        for e in iter_all_expressions(cond)
    )


def condition_uses_prior_signal(cond: Condition) -> bool:
    """True if `cond` (or anything nested in it) uses a prior_signal
    condition. prior_signal is the subset of Tier-3 that needs the
    iterative simulator to track *evaluated entry signals* (including
    skipped ones, scored by a phantom outcome) — see
    docs/design/v2-phase-a-stateful-conditions.md section 4.7.
    """
    return any(sub.type == "prior_signal" for sub in iter_conditions(cond))


def stateful_nesting_depth(cond: Condition) -> int:
    """Maximum count of nested regime_state conditions along any path.

    A bare regime_state is depth 1; a regime_state whose enter_when or
    exit_when contains another regime_state is depth 2. Non-stateful
    nesting (and/or/not/within_last_n_bars) does not add depth. Used by
    the validator to bound pathologically nested specs.
    """
    here = 1 if isinstance(cond, RegimeStateCondition) else 0
    children: list[Condition] = []
    if isinstance(cond, (AndCondition, OrCondition)):
        children = list(cond.conditions)
    elif isinstance(cond, (NotCondition, WithinLastNBarsCondition)):
        children = [cond.condition]
    elif isinstance(cond, RegimeStateCondition):
        children = [cond.enter_when, cond.exit_when]
    return here + max((stateful_nesting_depth(c) for c in children), default=0)


__all__ = [
    "T3_CONDITION_TYPES",
    "V2_CONDITION_TYPES",
    "condition_direct_expressions",
    "condition_uses_prior_signal",
    "condition_uses_stateful_v2",
    "condition_uses_tier3",
    "iter_all_expressions",
    "iter_conditions",
    "iter_expressions",
    "stateful_nesting_depth",
]
