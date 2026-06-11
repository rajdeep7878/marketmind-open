"""vectorbt-driven backtest execution.

`run_backtest(spec, start, end, initial_capital=10_000)` is the public
entry point. Steps:

  1. Pull historical OHLCV for the primary timeframe (and the filter
     timeframe if the spec uses one) via the cached market_data
     service.
  2. Hand the OHLCV to the translator to get SignalSet — entries,
     exits, stop-loss / take-profit / time-exit configuration,
     direction.
  3. Build a vectorbt.Portfolio:
       - price=open.shift(-1) so signals at bar t fill at bar t+1's
         open. This is the "no fill on the signal bar's close" rule
         we depend on for honest backtests.
       - fees + slippage from the spec's CostModel (or
         DEFAULT_COST_MODEL with a flag set on BacktestMeta).
       - size + size_type from position_sizing (or DEFAULT defaults).
       - sl_stop / tp_stop / td_stop / sl_trail wired from the
         SignalSet's exit config.
       - direction=long/short.
  4. Walk the trades and equity curve out of the Portfolio and
     return a BacktestRun.

Phase 3.1 explicitly does NOT compute Sharpe / drawdown / win-rate
metrics. The BacktestRun is the input to Phase 3.2's metrics module.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd
import structlog
import vectorbt as vbt
from marketmind_shared.schemas import (
    BacktestMeta,
    BacktestRun,
    EquityPoint,
    Trade,
)
from marketmind_shared.schemas.strategy_spec import (
    DEFAULT_COST_MODEL,
    DEFAULT_POSITION_SIZING,
    Condition,
    ConditionExit,
    ConditionFilter,
    CostModel,
    Direction,
    FixedPercentEquitySizing,
    FixedQuantitySizing,
    PositionSizing,
    StopLossAtrMultiple,
    StopLossFixedPrice,
    StopLossMethod,
    StopLossPercent,
    StopLossTrailingAtr,
    StopLossTrailingPercent,
    StrategySpec,
    TakeProfitAtrMultiple,
    TakeProfitFixedPrice,
    TakeProfitMethod,
    TakeProfitPercent,
    Timeframe,
)
from marketmind_shared.schemas.strategy_spec.introspection import condition_uses_tier3

from marketmind_workers.backtest import indicators as ind
from marketmind_workers.backtest.exit_attribution import attribute_exit
from marketmind_workers.backtest.fee_model import commission_for_spec, default_fee_model
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.session_filter import drop_weekends_in_data_dict
from marketmind_workers.backtest.slippage_model import (
    default_slippage_model,
    slippage_for_spec,
)
from marketmind_workers.backtest.translator import SignalSet, build_signals
from marketmind_workers.services.market_data import get_market_data

log = structlog.get_logger(__name__)


# Mapping for vectorbt's `freq` parameter — used by vbt for time-based
# stats. Timeframe enum values are already vbt-friendly strings, but
# vbt's tooling expects pandas frequency aliases for the daily case
# (Timeframe.D1.value == "1d" works; the rest match too).
_VBT_FREQ: dict[Timeframe, str] = {
    Timeframe.M1: "1min",
    Timeframe.M5: "5min",
    Timeframe.M15: "15min",
    Timeframe.M30: "30min",
    Timeframe.H1: "1h",
    Timeframe.H4: "4h",
    Timeframe.D1: "1D",
}


class BacktestError(Exception):
    """Raised for spec configurations the engine doesn't support yet."""


# ---- Public entry ---------------------------------------------------------


def run_backtest(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    initial_capital: float = 10_000.0,
    *,
    data_dir: str | Path = "/data",
    data_override: dict[Timeframe, pd.DataFrame] | None = None,
) -> BacktestRun:
    """Execute the spec across [start, end) and return a BacktestRun.

    `start` / `end` must be timezone-aware UTC. The engine fetches
    OHLCV from the cached Binance market-data service for the spec's
    primary timeframe (and filter timeframe if any), builds signals
    via the translator, and runs them through vectorbt.

    `data_dir` is the on-disk Parquet cache root — defaults to `/data`
    (the container mount). Override for host-side runs (`./data` from
    the repo root) so we don't try to write under `/data` directly.

    `data_override` short-circuits the market-data fetch. Phase 4's
    Monte Carlo permutation test passes synthetic OHLCV here; the
    dict must cover every Timeframe the spec consumes (primary + any
    filter). When provided, `data_dir` is ignored.
    """
    _require_utc("start", start)
    _require_utc("end", end)

    # ---- pull data ----
    if data_override is not None:
        data = data_override
    else:
        data = _load_required_data(spec, start, end, data_dir=data_dir)

    # ---- C.5 session filter: drop weekend bars when the instrument's
    # SessionHours.weekend_closed = True. Crypto specs (every pre-C.5
    # spec) have session_hours=None, the helper returns the SAME dict
    # reference — observationally bit-identical. The dispatch lands
    # BEFORE the Tier-3 router so iterative.py receives pre-dropped
    # data; no per-engine integration needed.
    data = drop_weekends_in_data_dict(data, spec)

    # ---- route Tier-3 specs to the iterative simulator ----
    # A spec using a prior_trade condition or a per-trade ratchet cannot
    # be evaluated by vectorbt's vectorised model — the custom sequential
    # engine in iterative.py handles it. Every other spec (v1 specs, and
    # v2 specs using only T1/T2 statefulness) stays on the vectorbt path
    # below, byte-for-byte unchanged.
    if _spec_uses_tier3(spec):
        return run_iterative_backtest(spec, data, start, end, initial_capital)

    primary_df = data[spec.primary_timeframe]

    # ---- build signals ----
    signals = build_signals(spec, data)

    # ---- cost model + sizing (with defaulted flags) ----
    cost_model, defaulted_costs = _resolve_costs(spec)
    position_sizing, defaulted_sizing = _resolve_sizing(spec.position_sizing)

    # ---- assemble vbt inputs ----
    size_value, size_type = _vbt_size(position_sizing, signals, primary_df)
    sl_stop, sl_trail = _vbt_stop_loss(signals.stop_loss, primary_df)
    tp_stop = _vbt_take_profit(signals.take_profit, signals.stop_loss, primary_df)

    # max_bars_held: vectorbt 0.28 doesn't have a native time-based
    # exit, so we inject one. For each entry at bar t, mark exits at
    # bar t + max_bars_held = True, then OR that with the existing
    # exits series. vbt's from_signals will pick whichever exit fires
    # first (stop / TP / signal / time-exit).
    exits = signals.exits
    if signals.max_bars_held is not None:
        time_exits = signals.entries.shift(signals.max_bars_held).fillna(False).astype(bool)
        exits = ind.as_series(exits | time_exits)

    # Fills at NEXT bar open: align the execution-price series to the
    # signal index but pull values from the next bar's open. The last
    # bar has no successor — NaN there means vbt simply skips a
    # signal that wants to fire on it. That's the honest answer.
    next_open = ind.column(primary_df, "open").shift(-1)

    direction_str = "longonly" if signals.direction is Direction.LONG else "shortonly"
    close = ind.column(primary_df, "close")

    portfolio = vbt.Portfolio.from_signals(
        close=close,
        entries=signals.entries,
        exits=exits,
        price=next_open,
        fees=cost_model.commission_pct,
        slippage=cost_model.slippage_pct,
        size=size_value,
        size_type=size_type,
        sl_stop=sl_stop,
        sl_trail=sl_trail,
        tp_stop=tp_stop,
        direction=direction_str,
        init_cash=initial_capital,
        freq=_VBT_FREQ[spec.primary_timeframe],
    )

    equity_curve = _equity_curve(portfolio)
    trades = _trades(
        portfolio,
        direction=signals.direction,
        signals=signals,
        primary_df=primary_df,
    )
    meta = BacktestMeta(
        symbol=spec.instrument.symbol,
        primary_timeframe=spec.primary_timeframe,
        filter_timeframe=spec.filter_timeframe,
        start=start,
        end=end,
        initial_capital=initial_capital,
        direction=signals.direction,
        defaulted_costs=defaulted_costs,
        defaulted_position_sizing=defaulted_sizing,
    )
    log.info(
        "backtest_complete",
        spec=spec.name,
        symbol=spec.instrument.symbol,
        primary_tf=spec.primary_timeframe.value,
        bars=len(primary_df),
        trades=len(trades),
    )
    return BacktestRun(
        spec_name=spec.name,
        meta=meta,
        equity_curve=equity_curve,
        trades=trades,
        entry_diagnostics=signals.entry_diagnostics,
    )


# ---- Helpers --------------------------------------------------------------


def _spec_uses_tier3(spec: StrategySpec) -> bool:
    """True if the spec uses a Tier-3 stateful element (a prior_trade
    condition, or a ratchet with reset='per_trade'); such specs must be
    routed to the iterative simulator rather than the vectorbt path.
    """
    conditions: list[Condition] = [spec.entry.condition]
    conditions.extend(e.condition for e in spec.exit.exits if isinstance(e, ConditionExit))
    conditions.extend(f.condition for f in spec.filters if isinstance(f, ConditionFilter))
    return any(condition_uses_tier3(c) for c in conditions)


def _require_utc(field_name: str, dt: datetime) -> None:
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise BacktestError(f"{field_name} must be timezone-aware UTC")


def _load_required_data(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    *,
    data_dir: str | Path = "/data",
) -> dict[Timeframe, pd.DataFrame]:
    """Pull OHLCV for the primary tf, plus any filter tf the spec
    declares. Per-timeframe fetches reuse the on-disk Parquet cache.
    """
    needed: list[Timeframe] = [spec.primary_timeframe]
    if spec.filter_timeframe is not None:
        needed.append(spec.filter_timeframe)
    out: dict[Timeframe, pd.DataFrame] = {}
    for tf in needed:
        df = get_market_data(spec.instrument.symbol, tf.value, start, end, data_dir=data_dir)
        if df.empty:
            raise BacktestError(
                f"no market data for {spec.instrument.symbol} {tf.value} in [{start}, {end})",
            )
        out[tf] = df
    return out


def _resolve_costs(
    spec: StrategySpec,
) -> tuple[CostModel, bool]:
    """Return (cost_model, defaulted_flag).

    Commission is derived from the FeeModel (Phase B.1, 2026-05-23) and
    slippage from the SlippageModel (Phase B.2, 2026-05-23) — both via
    table lookup on (spec.instrument.exchange, symbol, "taker"). The
    spec.costs path is no longer read for either field; the CostModel
    schema field stays for backward compatibility (serialisation, UI
    display) but the engine ignores its values.

    "Defaulted" here still means "the spec author didn't override
    costs" — the spec's costs equal DEFAULT_COST_MODEL. The UI flag
    surfaces "this run used our default cost models, your mileage with
    real fills may vary"; it's still meaningful because the FeeModel /
    SlippageModel defaults are themselves the v1 cost numbers, so the
    underlying provenance signal is preserved.
    """
    commission_pct = commission_for_spec(spec, side="taker", model=default_fee_model())
    slippage_pct = slippage_for_spec(spec, side="taker", model=default_slippage_model())
    defaulted = spec.costs == DEFAULT_COST_MODEL
    return CostModel(commission_pct=commission_pct, slippage_pct=slippage_pct), defaulted


def _resolve_sizing(
    sizing: PositionSizing | None,
) -> tuple[PositionSizing, bool]:
    if sizing is None or sizing == DEFAULT_POSITION_SIZING:
        return DEFAULT_POSITION_SIZING, True
    return sizing, False


def _vbt_size(
    sizing: PositionSizing,
    signals: SignalSet,
    primary_df: pd.DataFrame,
) -> tuple[float | pd.Series, str]:
    """Return (size, size_type) for vbt.Portfolio.from_signals.

    vbt's size_type vocabulary:
      - 'percent'  -> fraction of available cash (0..1)
      - 'amount'   -> number of base-currency units (fixed quantity)
      - 'value'    -> dollar amount (we don't use this in 3.1)

    For risk_based, the size at each entry depends on the stop
    distance — we compute size_pct = risk_percent / stop_pct as a
    Series. StopLossPercent / StopLossAtrMultiple / StopLossTrailingAtr
    are supported for this combination. StopLossTrailingPercent and
    StopLossFixedPrice are not supported yet (trailing percent has
    no well-defined "initial" stop distance; fixed-price needs
    entry-price tracking that lands in 3.2).

    StopLossTrailingAtr (added 2026-05-25, post-Hunt-7) uses the
    same initial-stop math as StopLossAtrMultiple — the trail only
    affects exit-side ratcheting, not entry-bar sizing. The position
    is sized off the FIRST stop distance (mult × ATR at entry); any
    later upward ratchet of the trailing stop reduces risk but does
    not retroactively resize. This matches the standard Turtle /
    trend-following position-sizing convention.
    """
    if isinstance(sizing, FixedPercentEquitySizing):
        return sizing.percent, "percent"
    if isinstance(sizing, FixedQuantitySizing):
        return sizing.quantity, "amount"
    # RiskBasedSizing
    stop = signals.stop_loss
    if stop is None:
        # Phase 1 cross-cutting validator should have prevented this,
        # but the runtime check stays as a safety net.
        raise BacktestError(
            "risk_based position sizing requires a stop_loss exit; "
            "Phase 1 validator should have rejected this spec",
        )
    if isinstance(stop, StopLossPercent):
        stop_pct = float(abs(stop.value))
        if stop_pct == 0.0:
            raise BacktestError("risk_based sizing requires non-zero stop_loss percent")
        return sizing.risk_percent / stop_pct, "percent"
    if isinstance(stop, StopLossAtrMultiple | StopLossTrailingAtr):
        # Identical sizing math for both: the INITIAL stop distance
        # at the entry bar is (atr_mult × ATR) regardless of whether
        # the stop subsequently trails or stays static. The trail
        # only affects exit-side behaviour.
        atr_series = ind.atr(primary_df, stop.atr_period)
        close = ind.column(primary_df, "close")
        # stop_distance_pct at bar t = (atr[t] * mult) / close[t]
        stop_pct_series = (atr_series * stop.mult) / close
        # Avoid div-by-zero on bars where ATR is NaN/0.
        size_series = sizing.risk_percent / stop_pct_series.where(stop_pct_series != 0)
        # Cap at 1.0 (no leverage in v1.0).
        clipped = size_series.clip(upper=1.0).fillna(0.0)
        return ind.as_series(clipped), "percent"
    raise BacktestError(
        f"risk_based sizing is not supported with stop method {type(stop).__name__} in Phase 3.1",
    )


def _vbt_stop_loss(
    stop: StopLossMethod | None,
    primary_df: pd.DataFrame,
) -> tuple[float | pd.Series | None, bool]:
    """Return (sl_stop_value, sl_trail_flag) for vbt.

    vbt's sl_stop is a percent (0..1). It also takes a Series if the
    stop varies bar-to-bar (atr-based). sl_trail=True flips the
    behaviour to trailing.
    """
    if stop is None:
        return None, False
    if isinstance(stop, StopLossPercent):
        return float(abs(stop.value)), False
    if isinstance(stop, StopLossTrailingPercent):
        return float(stop.value), True
    if isinstance(stop, StopLossAtrMultiple):
        atr_series = ind.atr(primary_df, stop.atr_period)
        close = ind.column(primary_df, "close")
        return ind.as_series((atr_series * stop.mult) / close), False
    if isinstance(stop, StopLossTrailingAtr):
        atr_series = ind.atr(primary_df, stop.atr_period)
        close = ind.column(primary_df, "close")
        return ind.as_series((atr_series * stop.mult) / close), True
    # The remaining StopLossMethod variant is StopLossFixedPrice
    # — convert to a percent-from-close approximation. Better
    # treatment lands in 3.2 (track entry price properly).
    assert isinstance(stop, StopLossFixedPrice)
    close = ind.column(primary_df, "close")
    pct = (close - stop.price).abs() / close
    return ind.as_series(pct), False


def _vbt_take_profit(
    tp: TakeProfitMethod | None,
    stop: StopLossMethod | None,
    primary_df: pd.DataFrame,
) -> float | pd.Series | None:
    """Return tp_stop value/Series for vbt.

    For r_multiple TPs we need the stop distance; we approximate it
    from the same StopLossMethod resolution as above.
    """
    if tp is None:
        return None
    if isinstance(tp, TakeProfitPercent):
        return float(tp.value)
    if isinstance(tp, TakeProfitFixedPrice):
        close = ind.column(primary_df, "close")
        return ind.as_series((tp.price - close).abs() / close)
    if isinstance(tp, TakeProfitAtrMultiple):
        # v1.2.E: tp_stop fraction = mult × ATR / close. vbt's
        # from_signals interprets tp_stop as a fraction of the entry
        # price; it flips the sign internally based on direction
        # ("longonly" vs "shortonly"), so the same positive fraction
        # works for both directions.
        close = ind.column(primary_df, "close")
        atr_series = ind.atr(primary_df, tp.atr_period)
        return ind.as_series(atr_series * tp.mult / close)
    # TakeProfitRMultiple
    if stop is None:
        raise BacktestError(
            "r_multiple take_profit requires a stop_loss exit "
            "(Phase 1 validator should have rejected this)",
        )
    sl_pct, _trail = _vbt_stop_loss(stop, primary_df)
    if sl_pct is None:
        raise BacktestError("r_multiple TP could not derive stop distance")
    if isinstance(sl_pct, pd.Series):
        return ind.as_series(sl_pct * tp.r)
    return float(sl_pct) * tp.r


# ---- BacktestRun extraction -----------------------------------------------


def _equity_curve(portfolio: vbt.Portfolio) -> list[EquityPoint]:
    """Extract the per-bar portfolio value as a list of EquityPoints."""
    value: pd.Series = portfolio.value()
    points: list[EquityPoint] = []
    for ts, v in value.items():
        if isinstance(ts, datetime):
            stamp = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
        else:
            # ts is a pd.Timestamp; convert
            stamp = pd.Timestamp(ts).to_pydatetime()  # type: ignore[arg-type]
        if math.isnan(float(v)):
            continue
        points.append(EquityPoint(timestamp=stamp, value=float(v)))
    return points


def _trades(
    portfolio: vbt.Portfolio,
    *,
    direction: Direction,
    signals: SignalSet,
    primary_df: pd.DataFrame,
) -> list[Trade]:
    """Walk vbt's trade records into our Trade list, attributing each
    exit to signal / stop_loss / take_profit / time / end_of_data via
    `exit_attribution.attribute_exit`.
    """
    records_df: pd.DataFrame = cast(
        "pd.DataFrame",
        portfolio.trades.records_readable,  # type: ignore[attr-defined]
    )
    if records_df.empty:
        return []

    out: list[Trade] = []
    for _idx, row in records_df.iterrows():
        entry_t = _to_utc_dt(row["Entry Timestamp"])
        exit_t = _to_utc_dt(row["Exit Timestamp"])
        entry_p = float(row["Avg Entry Price"])
        exit_p = float(row["Avg Exit Price"])
        size = float(row["Size"])
        pnl = float(row["PnL"])
        ret = float(row["Return"])
        status_val = row.get("Status")
        status_str = str(status_val) if isinstance(status_val, str) else ""
        if entry_p <= 0 or exit_p <= 0:
            continue
        exit_reason = attribute_exit(
            entry_time=cast("pd.Timestamp", pd.Timestamp(entry_t)),
            exit_time=cast("pd.Timestamp", pd.Timestamp(exit_t)),
            entry_price=entry_p,
            exit_price=exit_p,
            direction=direction,
            status=status_str,
            signals=signals,
            primary_df=primary_df,
        )
        out.append(
            Trade(
                entry_time=entry_t,
                exit_time=exit_t,
                entry_price=entry_p,
                exit_price=exit_p,
                size=size,
                pnl=pnl,
                return_pct=ret,
                direction=direction,
                exit_reason=exit_reason,
            ),
        )
    return out


def _to_utc_dt(value: Any) -> datetime:
    """Coerce a pandas Timestamp / datetime / string into a UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ts = pd.Timestamp(value)
    if ts.tz is None:
        ts = ts.tz_localize(UTC)
    # `Timestamp.to_pydatetime` returns NaT for missing values; vbt
    # doesn't emit NaT-indexed trade records, so the cast is safe.
    return cast("datetime", ts.to_pydatetime())


__all__ = ["BacktestError", "run_backtest"]


# Suppress unused-import warning for Sequence (kept for future expansion)
_ = Sequence
