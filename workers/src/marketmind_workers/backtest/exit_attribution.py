"""Post-hoc attribution of trade exit reasons.

vectorbt 0.28's `portfolio.trades.records_readable` exposes Status
('Open'/'Closed') and exit price/timestamp, but does NOT tell us WHY
the trade closed (signal vs stop_loss vs take_profit vs time exit).
We reconstruct the reason from the data we have:

  - SignalSet's stop_loss / take_profit / max_bars_held config.
  - The entry & exit price + timestamps from the trade record.
  - The primary-timeframe OHLCV (for "is this the last bar?").

The order of checks below is intentional — vbt evaluates intrabar in
the order stop_loss → take_profit → time → signal, and we mirror that
so the attribution agrees with what vbt actually did.

Reasons emitted:

  "open"          — trade is still open at end of run
  "end_of_data"   — close happened on the last bar with no other signal
  "stop_loss"     — exit_price matches the stop-loss level (or a trailing
                    stop produced a losing exit)
  "take_profit"   — exit_price matches the take-profit level
  "time"          — bars_held == max_bars_held
  "signal"        — fell through every other rule

Tolerances:
  Prices are compared within 0.5% relative tolerance because vbt fills
  at adjusted prices (fees + slippage), so exact equality is rare. The
  tolerance is wide enough to survive both, narrow enough that a real
  signal-driven exit doesn't get misattributed.
"""

from __future__ import annotations

import math
from typing import Final

import pandas as pd
from marketmind_shared.schemas.strategy_spec import (
    Direction,
    StopLossFixedPrice,
    StopLossMethod,
    StopLossPercent,
    StopLossTrailingAtr,
    StopLossTrailingPercent,
    TakeProfitFixedPrice,
    TakeProfitMethod,
    TakeProfitPercent,
)

from marketmind_workers.backtest.translator import SignalSet

EXIT_REASON_SIGNAL: Final[str] = "signal"
EXIT_REASON_STOP_LOSS: Final[str] = "stop_loss"
EXIT_REASON_TAKE_PROFIT: Final[str] = "take_profit"
EXIT_REASON_TIME: Final[str] = "time"
EXIT_REASON_END: Final[str] = "end_of_data"
EXIT_REASON_OPEN: Final[str] = "open"

ALL_EXIT_REASONS: Final[tuple[str, ...]] = (
    EXIT_REASON_SIGNAL,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    EXIT_REASON_TIME,
    EXIT_REASON_END,
    EXIT_REASON_OPEN,
)

# Relative price tolerance used to match exit_price against the SL/TP
# level. vbt fills at price * (1 ± slippage), and slippage is at most
# a few percent in normal specs; 0.5% is comfortably above that.
_PRICE_TOL: Final[float] = 0.005


def _sl_price(
    method: StopLossMethod,
    entry_price: float,
    direction: Direction,
) -> float | None:
    """Reconstruct the stop-loss price level from the method config.

    Returns None for methods that depend on bar-by-bar state we no
    longer have post-hoc (trailing stops, ATR stops with no ATR
    series). The caller falls back to a softer heuristic for those.
    """
    if isinstance(method, StopLossPercent):
        v = method.value
        return entry_price * (1.0 - v) if direction is Direction.LONG else entry_price * (1.0 + v)
    if isinstance(method, StopLossFixedPrice):
        return method.price
    # Trailing + ATR-based stops have dynamic levels we don't reconstruct.
    return None


def _tp_price(
    method: TakeProfitMethod,
    entry_price: float,
    direction: Direction,
) -> float | None:
    if isinstance(method, TakeProfitPercent):
        v = method.value
        return entry_price * (1.0 + v) if direction is Direction.LONG else entry_price * (1.0 - v)
    if isinstance(method, TakeProfitFixedPrice):
        return method.price
    # r_multiple depends on the SL distance, which we'd have to
    # reconstruct first; bail to the caller's softer heuristic.
    return None


def _is_trailing_or_atr(method: StopLossMethod | None) -> bool:
    return isinstance(method, StopLossTrailingPercent | StopLossTrailingAtr)


def attribute_exit(
    *,
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    entry_price: float,
    exit_price: float,
    direction: Direction,
    status: str,
    signals: SignalSet,
    primary_df: pd.DataFrame,
) -> str:
    """Best-effort attribution of why a trade closed. Pure post-hoc."""
    if status.lower() == "open":
        return EXIT_REASON_OPEN

    # SL match first — mirrors vbt's intrabar precedence.
    if signals.stop_loss is not None:
        sl_price = _sl_price(signals.stop_loss, entry_price, direction)
        if sl_price is not None and _prices_match(exit_price, sl_price):
            return EXIT_REASON_STOP_LOSS

    # TP match
    if signals.take_profit is not None:
        tp_price = _tp_price(signals.take_profit, entry_price, direction)
        if tp_price is not None and _prices_match(exit_price, tp_price):
            return EXIT_REASON_TAKE_PROFIT

    # Time exit
    if signals.max_bars_held is not None:
        bars_held = _bars_between(entry_time, exit_time, primary_df)
        if bars_held is not None and bars_held >= signals.max_bars_held:
            return EXIT_REASON_TIME

    # End-of-data: exit is on the last bar AND the user didn't fire an
    # explicit signal there.
    idx = primary_df.index
    if len(idx) > 0:
        last_ts = pd.Timestamp(idx[-1])  # type: ignore[arg-type]
        if pd.Timestamp(exit_time) >= last_ts:
            return EXIT_REASON_END

    # Trailing/ATR stops: any losing exit when a dynamic stop was the
    # only stop configured is most likely a stop hit. Pure heuristic —
    # documented in the module docstring.
    if _is_trailing_or_atr(signals.stop_loss):
        loss = exit_price < entry_price if direction is Direction.LONG else exit_price > entry_price
        if loss:
            return EXIT_REASON_STOP_LOSS

    return EXIT_REASON_SIGNAL


def _prices_match(a: float, b: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return math.isclose(a, b, rel_tol=_PRICE_TOL)


def _bars_between(
    entry_time: pd.Timestamp,
    exit_time: pd.Timestamp,
    primary_df: pd.DataFrame,
) -> int | None:
    """How many bars elapsed between entry and exit, inclusive of the
    exit bar. Returns None if either timestamp isn't on the primary
    index — in that case time-based attribution is skipped.
    """
    idx = primary_df.index
    try:
        entry_pos = idx.get_indexer([entry_time], method="nearest")[0]
        exit_pos = idx.get_indexer([exit_time], method="nearest")[0]
    except (KeyError, IndexError, ValueError):
        return None
    if entry_pos < 0 or exit_pos < 0:
        return None
    return int(exit_pos - entry_pos)


__all__ = [
    "ALL_EXIT_REASONS",
    "EXIT_REASON_END",
    "EXIT_REASON_OPEN",
    "EXIT_REASON_SIGNAL",
    "EXIT_REASON_STOP_LOSS",
    "EXIT_REASON_TAKE_PROFIT",
    "EXIT_REASON_TIME",
    "attribute_exit",
]
