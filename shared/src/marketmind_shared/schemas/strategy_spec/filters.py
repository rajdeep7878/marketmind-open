"""Trading filters: session / weekday / condition.

Filters gate when the strategy is even allowed to take entries.
session.hours_utc is a closed inclusive range [start, end]; midnight wrap
is not supported in v1.0 (use two filter entries).
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.strategy_spec.conditions import Condition


class SessionFilter(_StrictModel):
    type: Literal["session"] = "session"
    # Inclusive closed range. e.g. [13, 21] = 13:00 through 21:59 UTC.
    hours_utc: tuple[int, int]

    @model_validator(mode="after")
    def _validate_hours(self) -> Self:
        start, end = self.hours_utc
        if not (0 <= start <= 23):
            raise PydanticCustomError(
                "session_hours_invalid",
                "session.hours_utc[0] must be in [0,23], got {value}",
                {"value": start},
            )
        if not (0 <= end <= 23):
            raise PydanticCustomError(
                "session_hours_invalid",
                "session.hours_utc[1] must be in [0,23], got {value}",
                {"value": end},
            )
        if start > end:
            raise PydanticCustomError(
                "session_hours_invalid",
                "session.hours_utc must satisfy start <= end; "
                "wrap-around midnight is not supported in v1.0",
            )
        return self


class WeekdayFilter(_StrictModel):
    type: Literal["weekday"] = "weekday"
    # ISO 8601 weekday numbers: 1=Mon ... 7=Sun. At least one day required.
    days: list[int] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def _validate_days(self) -> Self:
        for d in self.days:
            if not (1 <= d <= 7):
                raise PydanticCustomError(
                    "weekday_day_invalid",
                    "weekday.days entries must be in [1,7] (ISO 8601), got {value}",
                    {"value": d},
                )
        if len(set(self.days)) != len(self.days):
            raise PydanticCustomError(
                "weekday_days_duplicate",
                "weekday.days must not contain duplicates",
            )
        return self


class ConditionFilter(_StrictModel):
    type: Literal["condition"] = "condition"
    condition: Condition


Filter = Annotated[
    SessionFilter | WeekdayFilter | ConditionFilter,
    Field(discriminator="type"),
]


__all__ = [
    "ConditionFilter",
    "Filter",
    "SessionFilter",
    "WeekdayFilter",
]
