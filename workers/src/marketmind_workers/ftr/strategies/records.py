"""DecisionRecord + ReasonCode — emitted for EVERY bar evaluated.

A strategy that stays flat all day still writes HOLD/SKIP records; the
decision log is the audit trail that makes gate-suppression statistics
(% of signals suppressed by the EV gate, by the overlay, ...) honest.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Action(StrEnum):
    ENTER_LONG = "ENTER_LONG"
    EXIT = "EXIT"
    HOLD = "HOLD"
    SKIP = "SKIP"


class ReasonCode(StrEnum):
    """Shared, extensible reason-code enum (mandate Stage 3)."""

    ENTER_EV_POSITIVE = "ENTER_EV_POSITIVE"
    SKIP_EV_NEGATIVE = "SKIP_EV_NEGATIVE"
    SKIP_COST_DOMINATES = "SKIP_COST_DOMINATES"
    SKIP_PROB_BELOW_MIN = "SKIP_PROB_BELOW_MIN"
    SKIP_LIQUIDITY_FILTER = "SKIP_LIQUIDITY_FILTER"
    SKIP_COOLDOWN = "SKIP_COOLDOWN"
    SKIP_GUARDRAIL = "SKIP_GUARDRAIL"
    SKIP_MAX_TRADES = "SKIP_MAX_TRADES"
    EXIT_PROB_DECAY = "EXIT_PROB_DECAY"
    EXIT_TRAIL_STOP = "EXIT_TRAIL_STOP"
    EXIT_SIGNAL_FLIP = "EXIT_SIGNAL_FLIP"
    EXIT_MAX_HOLD = "EXIT_MAX_HOLD"
    HOLD_IN_POSITION = "HOLD_IN_POSITION"
    HOLD_NO_SIGNAL = "HOLD_NO_SIGNAL"
    # Trend-portfolio entries are rule-confirmations, not EV estimates:
    ENTER_TREND_CONFIRMED = "ENTER_TREND_CONFIRMED"


class DecisionRecord(BaseModel):
    """One evaluated bar. Skips and holds included — always."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts_utc: datetime
    strategy_id: str
    symbol: str
    action: Action
    qty: Decimal = Decimal("0")
    expected_move_bps: float | None = None
    expected_cost_bps: float | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reason_codes: list[ReasonCode]
    feature_snapshot_hash: str = ""
    model_version: str = ""
    git_sha: str = ""

    @field_validator("ts_utc")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("ts_utc must be tz-aware UTC")
        return v

    @field_validator("reason_codes")
    @classmethod
    def _non_empty(cls, v: list[ReasonCode]) -> list[ReasonCode]:
        if not v:
            raise ValueError("every decision carries at least one reason code")
        return v
