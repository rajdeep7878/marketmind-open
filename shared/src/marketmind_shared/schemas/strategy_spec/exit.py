"""Exit conditions: stop_loss / take_profit / condition / time.

StopLossMethod and TakeProfitMethod are themselves discriminated unions
by `kind`. ExitCondition is discriminated by `type`. ExitRules wraps an
ordered list (first to trigger wins on a bar; list ordering breaks ties).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.strategy_spec.conditions import Condition

# ---- StopLossMethod variants ----------------------------------------------


class StopLossPercent(_StrictModel):
    kind: Literal["percent"] = "percent"
    # Range [-1, 1] excluding 0. Negative values are unusual but permitted so
    # the direction-consistency soft-warning logic can detect "long with stop
    # implied above entry" patterns (see validator.py).
    value: float = Field(ge=-1.0, le=1.0)


class StopLossAtrMultiple(_StrictModel):
    kind: Literal["atr_multiple"] = "atr_multiple"
    atr_period: int = Field(ge=2, le=100)
    mult: float = Field(gt=0.0, le=20.0)


class StopLossFixedPrice(_StrictModel):
    kind: Literal["fixed_price"] = "fixed_price"
    price: float = Field(gt=0.0)


class StopLossTrailingPercent(_StrictModel):
    kind: Literal["trailing_percent"] = "trailing_percent"
    value: float = Field(gt=0.0, le=1.0)


class StopLossTrailingAtr(_StrictModel):
    kind: Literal["trailing_atr"] = "trailing_atr"
    atr_period: int = Field(ge=2, le=100)
    mult: float = Field(gt=0.0, le=20.0)


StopLossMethod = Annotated[
    StopLossPercent
    | StopLossAtrMultiple
    | StopLossFixedPrice
    | StopLossTrailingPercent
    | StopLossTrailingAtr,
    Field(discriminator="kind"),
]


# ---- TakeProfitMethod variants --------------------------------------------


class TakeProfitPercent(_StrictModel):
    kind: Literal["percent"] = "percent"
    value: float = Field(gt=0.0, le=10.0)


class TakeProfitRMultiple(_StrictModel):
    kind: Literal["r_multiple"] = "r_multiple"
    # 0 < R <= 100. R=1 is "exit at 1x stop distance", below which the trade
    # is structurally a loser at break-even.
    r: float = Field(gt=0.0, le=100.0)


class TakeProfitFixedPrice(_StrictModel):
    kind: Literal["fixed_price"] = "fixed_price"
    price: float = Field(gt=0.0)


class TakeProfitAtrMultiple(_StrictModel):
    """v1.2.E (2026-05-25) — exit at a take-profit price expressed as a
    multiple of ATR(atr_period) at the entry bar. Symmetric to
    StopLossAtrMultiple — identical Pydantic bounds (atr_period 2..100,
    mult 0..20), identical direction-handling convention (the fraction
    is always positive; vbt's from_signals applies the sign based on
    the spec's direction).

    LONG: take_profit_price = entry_fill + mult × ATR_at_entry
    SHORT (vbt path only — iterative engine is long-only): vbt's
    tp_stop fraction is interpreted symmetrically; the engine flips
    the sign internally based on direction="shortonly".

    Surfaced as a v1.2 design-pass primitive (v1.2 design doc §4 v1.2.E)
    — symmetric to the existing StopLossAtrMultiple but in the
    take-profit direction. Common quant exit primitive (R-multiple
    targets where R is defined by recent volatility rather than a
    fixed stop distance).

    Stateless. Not v2 Tier-2 / Tier-3.
    """

    kind: Literal["atr_multiple"] = "atr_multiple"
    atr_period: int = Field(ge=2, le=100)
    mult: float = Field(gt=0.0, le=20.0)


TakeProfitMethod = Annotated[
    TakeProfitPercent | TakeProfitRMultiple | TakeProfitFixedPrice | TakeProfitAtrMultiple,
    Field(discriminator="kind"),
]


# ---- ExitCondition union --------------------------------------------------


class StopLossExit(_StrictModel):
    type: Literal["stop_loss"] = "stop_loss"
    method: StopLossMethod


class TakeProfitExit(_StrictModel):
    type: Literal["take_profit"] = "take_profit"
    method: TakeProfitMethod


class ConditionExit(_StrictModel):
    type: Literal["condition"] = "condition"
    condition: Condition


class TimeExit(_StrictModel):
    type: Literal["time"] = "time"
    max_bars_held: int = Field(ge=1, le=100_000)


class RMultipleExit(_StrictModel):
    """Primitive-4 (migration 0018) — a fixed risk-reward, ATR-anchored
    PRIMARY exit. Unlike a `condition`-type signal exit (which acts at bar
    close on an indicator flip), an R-multiple exit is meant to *hit* either
    its stop or its target intrabar — it is the strategy's core profit-taking
    + protective mechanic, not an auxiliary trend-flip exit.

    The R unit is one ATR multiple:

        R       = atr_multiple × ATR(atr_period)   (measured at the entry bar)
        stop    = entry − stop_R   × R
        target  = entry + target_R × R

    A classic 1:3 risk-reward is `stop_R=1, target_R=3` — the trade risks
    one R to make three. This wrapper composes the existing ATR-multiple
    stop + take-profit machinery: the engine synthesizes a
    StopLossAtrMultiple(mult = stop_R × atr_multiple) and a
    TakeProfitAtrMultiple(mult = target_R × atr_multiple), so the same
    proven at-entry ATR math, the same intrabar fill priority (stop before
    target), and the same vbt percent-of-close fractions drive it.

    BACKTEST-ONLY this phase: the live SpecTemplate trader is NOT taught to
    decompose this wrapper into a protective stop, so a trader spec using
    RMultipleExit would be rejected by spec_template's stop-loss requirement.
    That is acceptable and documented — the wrapper is for backtest research.

    Stateless. Not v2 Tier-2 / Tier-3. LONG via the iterative engine; SHORT
    via the vbt path (direction="shortonly"), symmetric to how
    StopLossAtrMultiple / TakeProfitAtrMultiple handle SHORT.
    """

    type: Literal["r_multiple"] = "r_multiple"
    atr_period: int = Field(ge=2, le=100, default=14)
    atr_multiple: float = Field(gt=0.0, le=20.0, default=1.0)
    # stop_R / target_R use the quant convention of capital-R for the
    # risk-unit multiplier (the strategy "risks 1R to make 3R"); the
    # mixedCase here is intentional domain naming, hence the N815 suppress.
    stop_R: float = Field(gt=0.0, le=100.0, default=1.0)  # noqa: N815
    target_R: float = Field(gt=0.0, le=100.0, default=3.0)  # noqa: N815


ExitCondition = Annotated[
    StopLossExit | TakeProfitExit | ConditionExit | TimeExit | RMultipleExit,
    Field(discriminator="type"),
]


class ExitRules(_StrictModel):
    # min_length=1: at least one exit is required by the spec.
    exits: list[ExitCondition] = Field(min_length=1)


def decompose_r_multiple(
    ex: RMultipleExit,
) -> tuple[StopLossAtrMultiple, TakeProfitAtrMultiple]:
    """Decompose an RMultipleExit into the synthesized ATR-multiple stop +
    take-profit the backtest engines actually run.

    R = atr_multiple × ATR(atr_period), so:
      stop_distance   = stop_R   × R = (stop_R   × atr_multiple) × ATR
      target_distance = target_R × R = (target_R × atr_multiple) × ATR

    Both synthesized methods share the wrapper's atr_period, so the engines'
    ATR series is computed once at that period (the `_atr_for_stop` /
    `_vbt_*` paths already resolve a single ATR period per position). Both
    legs are guaranteed in-bounds: stop_R/target_R ∈ (0, 100] and
    atr_multiple ∈ (0, 20], but StopLossAtrMultiple.mult is capped at 20, so
    the synthesized mult is clamped to the schema bound — a 100×20 product
    is far past any realistic R:R and the clamp keeps the synthesized method
    schema-valid without changing the common-case math (stop_R=1,
    target_R=3, atr_multiple=1 → mult 1 and 3, well within bounds).
    """
    stop_mult = min(ex.stop_R * ex.atr_multiple, 20.0)
    target_mult = min(ex.target_R * ex.atr_multiple, 20.0)
    stop = StopLossAtrMultiple(atr_period=ex.atr_period, mult=stop_mult)
    take_profit = TakeProfitAtrMultiple(atr_period=ex.atr_period, mult=target_mult)
    return stop, take_profit


__all__ = [
    "ConditionExit",
    "ExitCondition",
    "ExitRules",
    "RMultipleExit",
    "StopLossAtrMultiple",
    "StopLossExit",
    "StopLossFixedPrice",
    "StopLossMethod",
    "StopLossPercent",
    "StopLossTrailingAtr",
    "StopLossTrailingPercent",
    "TakeProfitAtrMultiple",
    "TakeProfitExit",
    "TakeProfitFixedPrice",
    "TakeProfitMethod",
    "TakeProfitPercent",
    "TakeProfitRMultiple",
    "TimeExit",
    "decompose_r_multiple",
]
