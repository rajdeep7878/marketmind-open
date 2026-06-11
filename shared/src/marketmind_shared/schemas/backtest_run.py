"""BacktestRun: the output of Phase 3.1's run_backtest.

Phase 3.1 stops at "we ran a spec against real data and got a portfolio
+ a trade list." The intentional scope:

  - equity_curve: timestamp -> portfolio value, for the whole bar range
  - trades: per-trade entry/exit times, prices, sizes, pnls
  - metadata: symbol, timeframe(s), date range, initial capital, plus
    flags for whether costs / sizing were defaulted from the schema
    defaults (so Phase 3.2's UI can flag those caveats next to results)

NOT computed here: Sharpe, max drawdown, win rate, profit factor, etc.
Those land in Phase 3.2's metrics module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import (
    Direction,
    Timeframe,
    _StrictModel,
)


class SignalDiagnosticsFailureMode(StrEnum):
    """Why the entry condition didn't produce trades, classified.

    NONE
        The condition produced at least one True signal during the
        run. Trades may still be 0 if exits or risk gates blocked
        every entry — but the entry signal itself is alive.
    CONDITIONS_NEVER_MET
        The entry condition evaluated to deterministic False on
        every bar (after warmup). The indicators warmed up cleanly;
        the comparison just never returned True. Likely a legitimate
        "strategy is too restrictive for this asset / window" result,
        OR a logically-degenerate spec (e.g. crossover between
        wildly different scales like price vs ATR).
    EVALUATION_DEGRADED
        A large fraction of bars (> the configured threshold) had
        NaN on at least one side of the comparison after the warmup
        window. The fillna(False) in build_signals then silently
        coerced them to False. This is almost always a bug in the
        spec (e.g. missing indicator source, params that produce
        NaN for the whole series) — NOT a real "no signals" result.
    """

    NONE = "none"
    CONDITIONS_NEVER_MET = "conditions_never_met"
    EVALUATION_DEGRADED = "evaluation_degraded"


class SignalDiagnostics(_StrictModel):
    """Counters captured during entry-condition evaluation.

    Recorded BEFORE the `.fillna(False).astype(bool)` step in
    `build_signals` so we can tell the difference between
    "deterministic False every bar" and "NaN every bar coerced to
    False." Surfaced on BacktestRun so the result page can flag
    degenerate specs that produce silently-zero-trade backtests.
    """

    bars_evaluated: int = Field(ge=0)
    # Pre-warmup bars where at least one side of the entry expression
    # was NaN (expected: every indicator has a warmup period). Not a
    # signal of failure on its own.
    nan_warmup_count: int = Field(ge=0)
    # Bars AFTER the longest indicator warmup window where at least one
    # side was still NaN. > 50% of post-warmup bars NaN ⇒ degraded.
    nan_post_warmup_count: int = Field(ge=0)
    # Bars where the entry condition evaluated to True (deterministic).
    true_count: int = Field(ge=0)
    # Bars where the entry condition evaluated to False (deterministic,
    # not NaN-coerced).
    deterministic_false_count: int = Field(ge=0)
    # Classification for the result page.
    failure_mode: SignalDiagnosticsFailureMode = SignalDiagnosticsFailureMode.NONE
    # The longest indicator warmup window (in bars) used to compute
    # `nan_post_warmup_count`. Kept for diagnostic transparency.
    warmup_bars: int = Field(ge=0)


def _require_utc(field_name: str, value: datetime) -> datetime:
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
    return value.astimezone(UTC)


class EquityPoint(_StrictModel):
    """One point on the equity curve."""

    timestamp: datetime
    value: float

    @field_validator("timestamp")
    @classmethod
    def _ts_utc(cls, v: datetime) -> datetime:
        return _require_utc("timestamp", v)


class Trade(_StrictModel):
    """One round-trip trade.

    `exit_reason` mirrors vectorbt's exit attribution (signal /
    stop_loss / take_profit / time / end). We string-typed it rather
    than enum'd so a new vbt release adding categories doesn't force
    a Pydantic schema bump.
    """

    entry_time: datetime
    exit_time: datetime
    entry_price: float = Field(gt=0.0)
    exit_price: float = Field(gt=0.0)
    size: float
    pnl: float
    return_pct: float
    direction: Direction
    exit_reason: str = Field(min_length=1, max_length=64)

    @field_validator("entry_time", "exit_time")
    @classmethod
    def _ts_utc(cls, v: datetime) -> datetime:
        return _require_utc("trade timestamp", v)


class BacktestMeta(_StrictModel):
    """Static metadata about the run.

    `defaulted_costs` / `defaulted_position_sizing` are booleans the
    UI surfaces next to the result: if the source didn't specify
    costs or sizing and the engine fell back to DEFAULT_COST_MODEL /
    DEFAULT_POSITION_SIZING, we tell the user.
    """

    symbol: str = Field(min_length=1, max_length=32)
    primary_timeframe: Timeframe
    filter_timeframe: Timeframe | None = None
    start: datetime
    end: datetime
    initial_capital: float = Field(gt=0.0)
    direction: Direction
    defaulted_costs: bool = False
    defaulted_position_sizing: bool = False

    @field_validator("start", "end")
    @classmethod
    def _ts_utc(cls, v: datetime) -> datetime:
        return _require_utc("meta timestamp", v)


class BacktestRun(_StrictModel):
    """Phase 3.1 contract: portfolio + trades + metadata.

    Pydantic-strict so it serialises cleanly to JSON for downstream
    storage. The equity_curve is a list-of-objects rather than a
    parallel pair of arrays because frontend code reads it more
    naturally that way and the size cost (~24 bytes/point JSON
    overhead) is negligible at backtest cadences.

    `entry_diagnostics` is optional (None on historical rows
    pre-Phase 5.2-ish that didn't compute it). When present it
    classifies why the trade count is what it is — load-bearing for
    distinguishing "strategy is too restrictive" from "spec is
    silently mis-extracted." See the SignalDiagnostics docstring.
    """

    spec_name: str = Field(min_length=1, max_length=200)
    schema_version: Literal["1.0"] = "1.0"
    meta: BacktestMeta
    equity_curve: list[EquityPoint]
    trades: list[Trade]
    entry_diagnostics: SignalDiagnostics | None = None


__all__ = [
    "BacktestMeta",
    "BacktestRun",
    "EquityPoint",
    "SignalDiagnostics",
    "SignalDiagnosticsFailureMode",
    "Trade",
]
