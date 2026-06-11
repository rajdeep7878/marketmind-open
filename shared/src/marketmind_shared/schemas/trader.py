"""Pydantic DTOs for MarketMind Trader v1.

Cross-service value types that both the API (read-only) and the
worker (writes + reads) consume. One Python model per persisted
table plus a handful of in-memory value types (SignalEvaluation,
BlockDecision, IndicatorSnapshot).

Conventions:
- Every monetary value (price / size / fee / cash / equity / PnL)
  is `decimal.Decimal`. Postgres `NUMERIC` columns round-trip
  losslessly via psycopg 3's default Decimal mapping.
- Every datetime is tz-aware UTC. The `_require_utc` helper mirrors
  the convention from `marketmind_shared.schemas.overfitting`.
- StrEnum values match the DB CHECK-constraint enums in
  `infra/db/migrations/0006..0010` exactly. Re-ordering or renaming
  a member here requires a corresponding migration.
- Strict base via `_StrictModel`: `extra='forbid'`, `frozen=True`,
  `validate_assignment=True`. Trader DTOs are descriptive — the
  worker writes new rows rather than mutating existing models.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import Timeframe, _StrictModel


def _require_utc(field_name: str, value: datetime) -> datetime:
    """UTC-aware datetime gate. Inlined rather than imported from
    `schemas.overfitting` because that helper is module-private.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise PydanticCustomError(
            "datetime_must_be_utc",
            "{field} must be timezone-aware UTC; got naive datetime",
            {"field": field_name},
        )
    if value.utcoffset() != timedelta(0):
        raise PydanticCustomError(
            "datetime_must_be_utc",
            "{field} must be UTC (offset 0)",
            {"field": field_name},
        )
    return value


# ---- Enums ------------------------------------------------------------------


class TemplateName(StrEnum):
    """Strategy template kinds. The `template` CHECK constraint on
    `trader_strategy_versions` is set in 0006 (the five v1 templates)
    and widened in 0012 to admit `spec` (the v2 generic executor, A.5a).
    """

    MA_TREND = "ma_trend"
    BREAKOUT = "breakout"
    RSI_MEAN_REVERSION = "rsi_mean_reversion"
    BB_MEAN_REVERSION = "bb_mean_reversion"
    VCB = "vcb"
    # v2 (A.5a): the generic StrategySpec executor — carries an extracted
    # spec in `parameters` and evaluates it through the shared backtest
    # condition evaluators. See workers/trader/templates/spec_template.py.
    SPEC = "spec"


class SignalKind(StrEnum):
    """Output of a StrategyTemplate.evaluate(). CHECK-constrained in 0008."""

    BUY = "BUY"
    SELL = "SELL"
    EXIT = "EXIT"
    HOLD = "HOLD"


class OrderSide(StrEnum):
    """v1 supports BUY / SELL only — long-only spot."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """v1 is market-only. The DB CHECK is single-value; the enum
    keeps the column open for future LIMIT / STOP types.
    """

    MARKET = "MARKET"


class OrderStatus(StrEnum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"


class PositionSide(StrEnum):
    """v1 is long-only spot."""

    LONG = "LONG"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class CloseReason(StrEnum):
    """How a position was closed. DB column is free-form TEXT (the audit
    trail tolerates additional values), but the executor only writes
    these four.
    """

    SIGNAL_EXIT = "signal_exit"
    STOP_HIT = "stop_hit"
    TAKE_PROFIT_HIT = "take_profit_hit"
    MANUAL = "manual"


class Severity(StrEnum):
    """Shared by RiskEvent and Alert. Filter logic: info -> log only,
    warning + critical -> Telegram (if configured).
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertChannel(StrEnum):
    TELEGRAM = "telegram"
    LOG = "log"


class RiskEventType(StrEnum):
    """The nine v1 risk event types. Order matters in the risk
    manager's short-circuit evaluation (see prompt Step 6).
    """

    BLOCK = "block"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_BREACH = "daily_loss_breach"
    WEEKLY_LOSS_BREACH = "weekly_loss_breach"
    STALE_DATA = "stale_data"
    VOLATILITY_REGIME = "volatility_regime"
    STRATEGY_DISABLED = "strategy_disabled"
    STRATEGY_NOT_PAPER_APPROVED = "strategy_not_paper_approved"
    DRIFT_BREACH = "drift_breach"


class HealthStatus(StrEnum):
    """Drift analyzer's classification. Advisory only in v1 — a
    `breach` triggers an alert but does not auto-disable.
    """

    HEALTHY = "healthy"
    WATCH = "watch"
    BREACH = "breach"


class LoopName(StrEnum):
    INGESTION = "ingestion"
    SIGNAL_EXECUTION = "signal_execution"
    # Step 12 collapses the per-loop heartbeat model into one
    # runner process that orchestrates all six phases per cycle.
    # The legacy values stay supported (old rows survive ALTERs)
    # but new bot-run rows always use 'runner'.
    RUNNER = "runner"


class RunStatus(StrEnum):
    RUNNING = "running"
    STOPPED = "stopped"
    CRASHED = "crashed"


# ---- Value-level types -----------------------------------------------------


# IndicatorSnapshot is a free-form mapping persisted to a JSONB column.
# Indicator values are derived stats — they live in float-space
# (pandas float64); Decimal would be over-precision and would round-trip
# through JSON as strings, complicating the read-side API responses.
IndicatorSnapshot = dict[str, float]


# ---- DTOs: market data + strategy --------------------------------------------


class Candle(_StrictModel):
    """One closed OHLCV bar. Maps 1:1 to a `trader_candles` row."""

    symbol: str = Field(min_length=1, max_length=32)
    timeframe: Timeframe
    open_ts: datetime
    close_ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    source: str = "ccxt"

    @field_validator("open_ts", "close_ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("candle timestamp", v)


class TraderStrategy(_StrictModel):
    """Logical strategy identity. Maps to a `trader_strategies` row."""

    id: UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("strategy timestamp", v)


class TraderStrategyVersion(_StrictModel):
    """Immutable snapshot of an approved StrategySpec.

    Maps 1:1 to a `trader_strategy_versions` row. The DB trigger
    enforces immutability of everything except enabled /
    approved_for_paper / notes. `approved_for_live` stays False
    in v1 — both Python and DB layers block flipping it.
    """

    id: UUID
    strategy_id: UUID
    version: int = Field(ge=1)
    marketmind_spec_id: UUID
    template: TemplateName
    parameters: dict[str, Any]
    symbols: list[str] = Field(min_length=1)
    timeframes: list[Timeframe] = Field(min_length=1)
    # Decimal proportions: 0.005 == 0.5%. Field bounds keep typo'd
    # values (e.g. 5 instead of 0.05) from passing validation.
    risk_pct: Decimal = Field(gt=Decimal(0), le=Decimal(1))
    fee_bps: Decimal = Field(ge=Decimal(0))
    slippage_bps: Decimal = Field(ge=Decimal(0))
    # JSONB blob snapshotted at approval time. The drift analyzer
    # consumes backtest_metrics["walk_forward"]["out_of_sample_*"];
    # the approve_paper admin endpoint validates that subtree is
    # populated before flipping the flag.
    backtest_metrics: dict[str, Any]
    overfitting_metrics: dict[str, Any] = Field(default_factory=dict)
    approved_for_paper: bool = False
    approved_for_live: bool = False
    enabled: bool = True
    notes: str = ""
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("created_at", v)


# ---- DTOs: signal + execution ----------------------------------------------


class SignalEvaluation(_StrictModel):
    """In-memory output of a StrategyTemplate.evaluate() call.

    Not a DB row directly: the signal engine turns non-HOLD
    evaluations into `trader_signals` rows; HOLD evaluations are
    audited but not persisted (they would dominate the table).
    """

    kind: SignalKind
    reason: str
    indicators: IndicatorSnapshot = Field(default_factory=dict)
    # The strategy's intent. The executor's actual fill price is
    # the next candle's open, transformed by slippage; this field
    # is the candle close that triggered the signal and is kept
    # for the audit trail.
    proposed_entry_price: Decimal
    proposed_stop_price: Decimal
    proposed_take_profit_price: Decimal | None = None


class Signal(_StrictModel):
    """Persisted signal. Maps to a `trader_signals` row."""

    id: UUID
    strategy_version_id: UUID
    symbol: str
    timeframe: Timeframe
    candle_close_ts: datetime
    signal: SignalKind
    reason: str
    indicators: IndicatorSnapshot
    proposed_entry_price: Decimal
    proposed_stop_price: Decimal
    proposed_take_profit_price: Decimal | None = None
    created_at: datetime
    processed_at: datetime | None = None

    @field_validator("candle_close_ts", "created_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("signal timestamp", v)

    @field_validator("processed_at")
    @classmethod
    def _utc_optional(cls, v: datetime | None) -> datetime | None:
        return _require_utc("processed_at", v) if v is not None else None


# ---- v2 stateful-condition persistence (A.5b) -----------------------------


class RegimeState(_StrictModel):
    """Persisted state of one `regime_state` condition — the latch value as
    of a given closed candle. The regime evaluates TRUE while `latched`.
    """

    latched: bool


class RatchetState(_StrictModel):
    """Persisted state of one `ratchet` expression — the running favorable
    extremum as of a given closed candle.

    `reset_epoch` is unused in A.5 (which persists `reset="never"` ratchets
    only); the field exists for A.6 forward-compatibility, where a
    `reset="per_trade"` ratchet records the trade-entry epoch its extremum
    resets at.
    """

    extremum: float = Field(allow_inf_nan=False)
    reset_epoch: int | None = None


# ---- v2 Tier-3 stateful-condition persistence (A.6) ------------------------
#
# A.6 runs `prior_signal` / `prior_trade` specs live by persisting the
# shadow Tier-3 simulation's checkpoint (design doc §6C). These models are
# the JSONB-persistence mirror of `backtest.trade_history` /
# `backtest.iterative`'s in-memory dataclasses — `marketmind_shared` cannot
# import `marketmind_workers`, so a string `outcome` stands in for the
# `TradeOutcome` StrEnum.


class Tier3SignalRecord(_StrictModel):
    """One evaluated entry signal in the live SignalHistory. `outcome` /
    `return_pct` / `resolved_bar` stay None while the signal's real or
    phantom trade is pending — `prior_signal` consults resolved records
    only (the look-ahead gate, design doc §6C.3).
    """

    signal_bar: int
    fired: bool
    return_pct: float | None = Field(default=None, allow_inf_nan=False)
    outcome: Literal["win", "loss", "breakeven"] | None = None
    resolved_bar: int | None = None


class Tier3CompletedTrade(_StrictModel):
    """One completed shadow-simulation trade — what `prior_trade` gates on.
    Recorded in close order.
    """

    entry_index: int
    exit_index: int
    return_pct: float = Field(allow_inf_nan=False)
    outcome: Literal["win", "loss", "breakeven"]


class Tier3ShadowPosition(_StrictModel):
    """An open position inside the shadow Tier-3 simulation.

    `size` is stored even though Tier-3 *gating* is size-independent: a
    reloaded position must keep its exact size so `return_pct` is computed
    bit-for-bit identically across cycles — `(s·a − s·b)/(s·b)` is not the
    float-exact equal of `(a − b)/b`.
    """

    entry_bar: int
    entry_fill: float = Field(allow_inf_nan=False)
    size: float = Field(allow_inf_nan=False)
    stop_level: float | None = Field(default=None, allow_inf_nan=False)
    tp_level: float | None = Field(default=None, allow_inf_nan=False)
    trail_anchor: float = Field(allow_inf_nan=False)


class Tier3PendingPhantom(_StrictModel):
    """A skipped signal's phantom trade, mid-simulation (design doc §6C.3):
    a mini-position advanced one bar per cycle until its exit fires, at
    which point the phantom resolves and its SignalHistory record fills in.

    `position` is None for a phantom skipped on the latest bar — it has not
    filled yet (a phantom fills at `signal_bar + 1`'s open).
    """

    signal_bar: int
    position: Tier3ShadowPosition | None = None
    pending_exit_bar: int | None = None


class Tier3State(_StrictModel):
    """The Tier-3 checkpoint persisted in `StrategyState.tier3` — the live
    shadow simulation's full state as of a closed candle (design doc §6C).
    Absent (None) for every Tier-1 / Tier-2 spec.
    """

    signal_history: list[Tier3SignalRecord] = Field(default_factory=list)
    trade_history: list[Tier3CompletedTrade] = Field(default_factory=list)
    shadow_position: Tier3ShadowPosition | None = None
    pending_entry_bar: int | None = None
    pending_exit_bar: int | None = None
    pending_phantoms: list[Tier3PendingPhantom] = Field(default_factory=list)
    trade_id: int = 0
    # Absolute bar index this checkpoint is as-of — the live stepper
    # resumes at last_bar + 1. -1 on a cold start (nothing processed yet).
    last_bar: int = -1
    # The shadow simulation's notional cash — evolved exactly as the
    # iterative backtest evolves it, so position sizes (and therefore
    # `pnl`) match it bit-for-bit. Tier-3 *gating* is size-independent;
    # cash is tracked only for that drift-parity fidelity.
    cash: float = Field(default=10_000.0, allow_inf_nan=False)


class StrategyState(_StrictModel):
    """The `trader_strategy_state.state` JSONB payload — the stateful
    evaluation state of one `(strategy_version, symbol, timeframe)` as of a
    closed candle.

    `regimes` / `ratchets` are positional: the i-th entry is the i-th
    `regime_state` / `ratchet` node in deterministic condition-tree walk
    order (design doc §6B.5). A trader version's spec is immutable, so the
    positional mapping is stable for the version's lifetime.

    `tier3` carries the live Tier-3 shadow-simulation checkpoint (A.6); it
    is None for Tier-1/Tier-2 specs. A row whose state has a non-None
    `tier3` is written with `state_schema_version = 2`, else 1.
    """

    regimes: list[RegimeState] = Field(default_factory=list)
    ratchets: list[RatchetState] = Field(default_factory=list)
    tier3: Tier3State | None = None


class PaperOrder(_StrictModel):
    """Maps to a `trader_paper_orders` row."""

    id: UUID
    signal_id: UUID
    strategy_version_id: UUID
    symbol: str
    side: OrderSide
    order_type: OrderType = OrderType.MARKET
    requested_size: Decimal = Field(gt=Decimal(0))
    requested_at: datetime
    status: OrderStatus
    rejection_reason: str | None = None
    intended_fill_ts: datetime

    @field_validator("requested_at", "intended_fill_ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("order timestamp", v)


class PaperFill(_StrictModel):
    """Maps to a `trader_paper_fills` row."""

    id: UUID
    order_id: UUID
    fill_ts: datetime
    fill_price: Decimal = Field(gt=Decimal(0))
    size: Decimal = Field(gt=Decimal(0))
    fee: Decimal = Field(ge=Decimal(0))
    slippage_bps_applied: Decimal = Field(ge=Decimal(0))
    notional: Decimal = Field(gt=Decimal(0))

    @field_validator("fill_ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("fill_ts", v)


class PaperPosition(_StrictModel):
    """Maps to a `trader_paper_positions` row.

    The on-DB partial UNIQUE INDEX guarantees at most one OPEN row
    per `(strategy_version_id, symbol)`. Closed positions stack
    freely across time.
    """

    id: UUID
    strategy_version_id: UUID
    symbol: str
    side: PositionSide = PositionSide.LONG
    entry_order_id: UUID
    exit_order_id: UUID | None = None
    entry_price: Decimal = Field(gt=Decimal(0))
    entry_ts: datetime
    exit_price: Decimal | None = None
    exit_ts: datetime | None = None
    size: Decimal = Field(gt=Decimal(0))
    # Trader invariant: no stop = no trade. The strategy template /
    # risk manager guarantees this before any open ever reaches the
    # INSERT.
    stop_price: Decimal = Field(gt=Decimal(0))
    take_profit_price: Decimal | None = None
    status: PositionStatus
    realised_pnl: Decimal | None = None
    realised_pnl_pct: Decimal | None = None
    close_reason: CloseReason | None = None

    @field_validator("entry_ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("entry_ts", v)

    @field_validator("exit_ts")
    @classmethod
    def _utc_optional(cls, v: datetime | None) -> datetime | None:
        return _require_utc("exit_ts", v) if v is not None else None


# ---- DTOs: portfolio + risk + alerts ---------------------------------------


class PortfolioSnapshot(_StrictModel):
    """Maps to a `trader_portfolio_snapshots` row. One per
    signal-execution cycle.
    """

    id: int
    ts: datetime
    cash: Decimal
    equity: Decimal
    unrealised_pnl: Decimal
    realised_pnl_cumulative: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    drawdown_pct: Decimal
    open_positions_count: int = Field(ge=0)
    per_strategy_breakdown: dict[str, Any] = Field(default_factory=dict)
    per_symbol_breakdown: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("ts", v)


class RiskEvent(_StrictModel):
    """Maps to a `trader_risk_events` row. The risk manager writes
    one row per block decision in the same transaction that
    produces the associated alert.
    """

    id: UUID
    ts: datetime
    event_type: RiskEventType
    severity: Severity
    strategy_version_id: UUID | None = None
    symbol: str | None = None
    signal_id: UUID | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("ts", v)


class BlockDecision(_StrictModel):
    """In-memory output of `risk.evaluate_risk(...)`. Not persisted
    directly: an `approved` decision flows into an order; a
    `blocked` decision wrote the RiskEvent row whose id is here.
    """

    kind: Literal["approved", "blocked"]
    # Set when approved. May be smaller than the strategy's request
    # after per-trade / portfolio-risk clipping.
    size: Decimal | None = None
    # Set when blocked.
    reason: str | None = None
    event_type: RiskEventType | None = None
    risk_event_id: UUID | None = None


class Alert(_StrictModel):
    """Maps to a `trader_alerts` row.

    The DB row is the source of truth — written even when the
    network delivery fails. `delivered` / `delivery_error` are
    operational metadata.
    """

    id: UUID
    ts: datetime
    channel: AlertChannel
    severity: Severity
    subject: str
    body: str
    delivered: bool = False
    delivery_error: str | None = None

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("ts", v)


# ---- DTOs: ops -------------------------------------------------------------


class DriftMetric(_StrictModel):
    """Maps to a `trader_drift_metrics` row. Computed daily per
    strategy version; advisory only in v1 (`breach` alerts but
    does not auto-disable).

    `window_label` carries the human-readable window descriptor
    (`7d`, `30d`, `all`). The column name carries the `_label`
    suffix because `window` is a Postgres reserved word.
    """

    id: UUID
    ts: datetime
    strategy_version_id: UUID
    window_label: str
    paper_trade_count: int = Field(ge=0)
    paper_win_rate: Decimal
    paper_avg_return_per_trade: Decimal
    paper_current_drawdown_pct: Decimal
    backtest_trade_freq_per_week: Decimal
    backtest_win_rate: Decimal
    backtest_avg_return_per_trade: Decimal
    backtest_max_drawdown_pct: Decimal
    trade_freq_ratio: Decimal
    win_rate_delta: Decimal
    avg_return_delta: Decimal
    drawdown_ratio: Decimal
    health_status: HealthStatus

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("ts", v)


class BotRun(_StrictModel):
    """Maps to a `trader_bot_runs` row. One row per loop instance;
    `last_heartbeat_at` touched every iteration.
    """

    id: UUID
    loop_name: LoopName
    started_at: datetime
    last_heartbeat_at: datetime
    status: RunStatus
    worker_id: str
    notes: str = ""

    @field_validator("started_at", "last_heartbeat_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("bot_run timestamp", v)


class AuditLog(_StrictModel):
    """Maps to a `trader_audit_logs` row. Structured append-only
    event log; complements stdout structlog.
    """

    id: int
    ts: datetime
    actor: str
    event: str
    entity_type: str
    entity_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("ts", v)


__all__ = [
    "Alert",
    "AlertChannel",
    "AuditLog",
    "BlockDecision",
    "BotRun",
    "Candle",
    "CloseReason",
    "DriftMetric",
    "HealthStatus",
    "IndicatorSnapshot",
    "LoopName",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PaperFill",
    "PaperOrder",
    "PaperPosition",
    "PortfolioSnapshot",
    "PositionSide",
    "PositionStatus",
    "RatchetState",
    "RegimeState",
    "RiskEvent",
    "RiskEventType",
    "RunStatus",
    "Severity",
    "Signal",
    "SignalEvaluation",
    "SignalKind",
    "StrategyState",
    "TemplateName",
    "Tier3CompletedTrade",
    "Tier3PendingPhantom",
    "Tier3ShadowPosition",
    "Tier3SignalRecord",
    "Tier3State",
    "TraderStrategy",
    "TraderStrategyVersion",
]
