"""Tests for the breakout strategy template.

Realistic synthetic series: a flat range followed by a clean breakout
gives an unambiguous "close > prior-N high" signal, and the trend EMA
warms to ~the flat-range level so the trend filter passes after the
breakout. Symmetric pattern for the EXIT path (close < prior-M low).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pandas as pd
import pytest
from marketmind_workers.backtest import indicators as ind
from marketmind_workers.trader.templates.base import atr_stop_for_long
from marketmind_workers.trader.templates.breakout import (
    BreakoutParams,
    BreakoutTemplate,
)
from pydantic import ValidationError

_BREAKOUT = 5
_EXIT = 3
_TREND = 10
_ATR = 5
_MULT = Decimal("2.0")


def _params() -> BreakoutParams:
    return BreakoutParams(
        breakout_period=_BREAKOUT,
        exit_period=_EXIT,
        trend_ema_period=_TREND,
        atr_period=_ATR,
        atr_mult=_MULT,
    )


def _open_position_at(price: Decimal, stop: Decimal):  # type: ignore[no-untyped-def]
    from marketmind_shared.schemas.trader import PaperPosition, PositionStatus

    return PaperPosition(
        id=uuid4(),
        strategy_version_id=uuid4(),
        symbol="BTC/USDT",
        entry_order_id=uuid4(),
        entry_price=price,
        entry_ts=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
        size=Decimal("0.1"),
        stop_price=stop,
        status=PositionStatus.OPEN,
    )


def test_params_reject_zero_breakout_period() -> None:
    with pytest.raises(ValidationError):
        BreakoutParams(breakout_period=1)


def test_min_bars_needed_uses_longest_window() -> None:
    t = BreakoutTemplate(_params())
    # max(5, 3, 10, 5) + 5 = 15
    assert t.min_bars_needed() == 15


def test_evaluate_buys_on_close_above_prior_n_high(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """20 flat bars at 100, then close = 105: prior-5 high is 100.1
    (from constant series high = close * 1.001), close (105) breaks it.
    Trend EMA(10) at bar 20 ≈ 100.91 ⇒ close > trend ⇒ BUY.
    """
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0] * 20 + [105.0]
    df = make_candles(closes)
    template = BreakoutTemplate(_params())
    result = template.evaluate(df, position=None)

    assert result.kind is SignalKind.BUY
    expected_atr = float(ind.atr(df, _ATR).iloc[-1])
    expected_stop = atr_stop_for_long(
        Decimal("105"),
        Decimal(str(expected_atr)),
        _MULT,
    )
    assert result.proposed_entry_price == Decimal("105")
    assert result.proposed_stop_price == expected_stop
    assert set(result.indicators.keys()) == {"prior_high", "prior_low", "ema_trend", "atr"}


def test_evaluate_holds_when_no_breakout(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """Constant 100 ⇒ close = prior high, not strictly greater ⇒ HOLD."""
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 30)
    template = BreakoutTemplate(_params())
    result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_exits_on_close_below_prior_m_low(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """20 flat at 100, breakout to 110, then drop to 90: with an open
    position, close < prior-3 low ⇒ EXIT.
    """
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0] * 20 + [110.0, 108.0, 105.0, 90.0]
    df = make_candles(closes)
    position = _open_position_at(Decimal("110"), Decimal("100"))
    template = BreakoutTemplate(_params())
    result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.EXIT
    # EXIT carries the position's stop for the audit trail.
    assert result.proposed_stop_price == Decimal("100")


def test_evaluate_holds_when_position_open_and_no_exit_signal(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """Position open, close above prior low ⇒ HOLD (ride the trend)."""
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0] * 20 + [110.0, 112.0, 115.0]
    df = make_candles(closes)
    position = _open_position_at(Decimal("110"), Decimal("100"))
    template = BreakoutTemplate(_params())
    result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.HOLD


def test_evaluate_is_deterministic(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    closes = [100.0] * 20 + [105.0]
    df = make_candles(closes)
    template = BreakoutTemplate(_params())
    first = template.evaluate(df, position=None)
    second = template.evaluate(df, position=None)
    assert first == second
