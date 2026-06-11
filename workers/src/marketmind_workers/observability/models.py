"""Daily summary report — the data model.

The JSON file (one per day) is the source of truth; the rendered text is
derived from it. These Pydantic models define the JSON shape and give the
report a schema to validate against. All money is GBP (the paper bot
trades in £); all times are UTC.

Every field has a default or is nullable so a partial snapshot — a
bot-down state, a strategy with no history yet — still produces a valid
report rather than crashing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: str = "1.0"

BotStatus = Literal["HEALTHY", "DEGRADED", "DOWN"]
StrategyStatus = Literal["WARMUP", "EVALUATING", "IN_POSITION", "DISABLED"]


class BotHealth(BaseModel):
    """Heartbeat freshness + trailing-24h cycle activity."""

    model_config = ConfigDict(extra="forbid")

    status: BotStatus
    heartbeat_age_seconds: float | None = None
    heartbeat_fresh: bool = False
    cycles_24h: int = 0
    signal_cycles_24h: int = 0
    errors_24h: int = 0


class EquitySummary(BaseModel):
    """Portfolio equity now, the 24h change, and all-time P&L (GBP)."""

    model_config = ConfigDict(extra="forbid")

    current_gbp: float | None = None
    change_24h_gbp: float | None = None
    change_24h_pct: float | None = None
    open_positions: int = 0
    closed_trades_24h: int = 0
    all_time_pnl_gbp: float | None = None
    all_time_since: str | None = None  # YYYY-MM-DD of the first snapshot


class StrategySummary(BaseModel):
    """One strategy version's state and trailing-24h activity."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    template: str
    timeframe: str
    symbol: str
    status: StrategyStatus
    last_decision: str | None = None  # HOLD | BUY | EXIT | ...
    last_cycle_age_hours: float | None = None
    bars_have: int | None = None
    bars_needed: int | None = None
    state_rows: int = 0  # trader_strategy_state rows — 0 for v1 / non-stateful
    trades_24h: int = 0


class DailySummary(BaseModel):
    """The full daily snapshot — the JSON file's shape."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    date: str  # YYYY-MM-DD (UTC)
    generated_at: datetime
    bot_health: BotHealth
    equity: EquitySummary
    strategies: list[StrategySummary] = Field(default_factory=list)
    risk_events_24h: int = 0
    drift_events_24h: int = 0
    idempotency_guard_hits_24h: int = 0
    disable_alert_events_24h: int = 0
    notes: list[str] = Field(default_factory=list)


__all__ = [
    "SCHEMA_VERSION",
    "BotHealth",
    "BotStatus",
    "DailySummary",
    "EquitySummary",
    "StrategyStatus",
    "StrategySummary",
]
