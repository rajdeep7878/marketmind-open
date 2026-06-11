"""Position sizing: fixed_percent_equity / risk_based / fixed_quantity.

Discriminated by `mode`. The cross-cutting rule "risk_based requires
stop_loss" lives on StrategySpec, not here, because it depends on the
exit configuration.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from marketmind_shared.schemas.strategy_spec.common import _StrictModel


class FixedPercentEquitySizing(_StrictModel):
    mode: Literal["fixed_percent_equity"] = "fixed_percent_equity"
    # (0, 1]. v1.0 forbids leverage, so >1.0 is disallowed.
    percent: float = Field(gt=0.0, le=1.0)


class RiskBasedSizing(_StrictModel):
    mode: Literal["risk_based"] = "risk_based"
    # Conservative ceiling at 10%; serious-money strategies are usually <2%.
    risk_percent: float = Field(gt=0.0, le=0.1)


class FixedQuantitySizing(_StrictModel):
    mode: Literal["fixed_quantity"] = "fixed_quantity"
    # Unbounded above — quantity meaning depends on the instrument
    # (one BTC vs one DOGE). The pre-trade check in the backtester will
    # reject orders that exceed available equity.
    quantity: float = Field(gt=0.0)


PositionSizing = Annotated[
    FixedPercentEquitySizing | RiskBasedSizing | FixedQuantitySizing,
    Field(discriminator="mode"),
]


# Default per docs/strategy-spec.md: 100% of equity, no leverage.
DEFAULT_POSITION_SIZING: FixedPercentEquitySizing = FixedPercentEquitySizing(
    mode="fixed_percent_equity",
    percent=1.0,
)


__all__ = [
    "DEFAULT_POSITION_SIZING",
    "FixedPercentEquitySizing",
    "FixedQuantitySizing",
    "PositionSizing",
    "RiskBasedSizing",
]
