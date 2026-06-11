"""EntryRules: condition + order_type + optional limit_offset_pct.

Per docs/strategy-spec.md, limit_offset_pct is conditionally required:
present iff order_type == "limit", and bounded to ±5% when present.
Enforced as a model-level constraint with stable error codes so the UI
can highlight the offending field.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import OrderType, _StrictModel
from marketmind_shared.schemas.strategy_spec.conditions import Condition

_LIMIT_OFFSET_MIN = -0.05
_LIMIT_OFFSET_MAX = 0.05


class EntryRules(_StrictModel):
    condition: Condition
    order_type: OrderType
    limit_offset_pct: float | None = Field(
        default=None,
        ge=_LIMIT_OFFSET_MIN,
        le=_LIMIT_OFFSET_MAX,
    )

    @model_validator(mode="after")
    def _validate_limit_offset(self) -> Self:
        if self.order_type is OrderType.LIMIT and self.limit_offset_pct is None:
            raise PydanticCustomError(
                "limit_offset_required",
                "limit_offset_pct is required when order_type is 'limit'",
            )
        if self.order_type is OrderType.MARKET and self.limit_offset_pct is not None:
            raise PydanticCustomError(
                "limit_offset_forbidden",
                "limit_offset_pct must be absent when order_type is 'market'",
            )
        return self


__all__ = ["EntryRules"]
