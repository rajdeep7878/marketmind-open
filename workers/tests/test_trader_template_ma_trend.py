"""Tests for the ma_trend strategy template.

Most scenarios use realistic synthetic candle series: 20 flat bars
followed by 5 climbing bars is enough for fast EMA(2) to cross slow
EMA(4) cleanly, and the trend EMA(10) reacts slowly enough that the
trend filter passes. The Decimal stop math is verified against the
shared `atr_stop_for_long` helper rather than a literal constant to
keep the test robust against minor `ta`-library quantisation drift.
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
from marketmind_workers.trader.templates.ma_trend import (
    MaTrendParams,
    MaTrendTemplate,
)
from pydantic import ValidationError

# Small windows used throughout — keeps fixtures short and the
# crossover math tractable for inspection.
_FAST = 2
_SLOW = 4
_TREND = 10
_ATR = 5
_MULT = Decimal("2.0")


def _params() -> MaTrendParams:
    return MaTrendParams(
        fast_ema_period=_FAST,
        slow_ema_period=_SLOW,
        trend_ema_period=_TREND,
        atr_period=_ATR,
        atr_mult=_MULT,
    )


def _open_position_at(price: Decimal, stop: Decimal):  # type: ignore[no-untyped-def]
    """Construct a minimally-valid PaperPosition for EXIT-path tests."""
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


def test_params_reject_fast_geq_slow() -> None:
    with pytest.raises(ValidationError, match="fast_must_be_less_than_slow"):
        MaTrendParams(fast_ema_period=10, slow_ema_period=10)


def test_min_bars_needed_uses_longest_window() -> None:
    t = MaTrendTemplate(_params())
    # max(slow=4, trend=10, atr=5) + 5 buffer = 15
    assert t.min_bars_needed() == 15


def test_evaluate_holds_on_constant_series_no_cross(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """All-constant prices ⇒ fast == slow ⇒ no cross ⇒ HOLD."""
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 30)
    template = MaTrendTemplate(_params())
    result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_buys_on_clean_cross_with_trend_up(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """20 flat bars at 100, then close = 105 at bar 20: fast EMA jumps
    from 100 to ~103.33, slow EMA to ~102 — the upward cross fires on
    this exact bar. Trend EMA(10) at bar 20 ≈ 100.91, so close (105)
    is above it. Template fires BUY on the cross bar.

    Note: signals fire ON THE CROSS BAR. Subsequent bars (where both
    EMAs are already above) produce HOLD because there's no fresh
    cross event to detect.
    """
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0] * 20 + [105.0]
    df = make_candles(closes)
    template = MaTrendTemplate(_params())
    result = template.evaluate(df, position=None)

    assert result.kind is SignalKind.BUY
    assert result.reason.startswith("fast EMA crossed above slow")
    # Re-derive the expected stop from the centralised helper +
    # the live ATR value so the test survives minor `ta`-library
    # precision drift.
    expected_atr = float(ind.atr(df, _ATR).iloc[-1])
    expected_stop = atr_stop_for_long(
        Decimal("105"),
        Decimal(str(expected_atr)),
        _MULT,
    )
    assert result.proposed_entry_price == Decimal("105")
    assert result.proposed_stop_price == expected_stop
    # Snapshot must carry the four expected indicator keys.
    assert set(result.indicators.keys()) == {"ema_fast", "ema_slow", "ema_trend", "atr"}


def test_evaluate_holds_when_no_position_and_no_cross(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """Slow steady decline: fast leads slow downward, no upward cross
    in the most recent two bars ⇒ HOLD (no position).
    """
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0 - 0.1 * i for i in range(30)]
    df = make_candles(closes)
    template = MaTrendTemplate(_params())
    result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_exits_on_opposite_cross_with_open_position(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """20 flat + 5 up + 2 down: fast crosses BELOW slow on the second
    down bar (bar 26 — fast 113.61 vs slow 115.31). With an open
    position ⇒ EXIT on that exact bar. Same cross-on-the-bar rule as
    the BUY test: this fixture ends at the cross.
    """
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0] * 20 + [105.0, 110.0, 115.0, 120.0, 125.0] + [120.0, 110.0]
    df = make_candles(closes)
    position = _open_position_at(Decimal("125"), Decimal("115"))
    template = MaTrendTemplate(_params())
    result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.EXIT
    assert result.reason.startswith("fast EMA crossed below slow")
    # EXIT carries the position's stop forward for the audit trail.
    assert result.proposed_stop_price == Decimal("115")


def test_evaluate_holds_when_position_open_and_no_opposite_cross(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """Same uptrend that triggered BUY, but a position is already
    open ⇒ HOLD (we don't re-enter; we wait for the opposite cross).
    """
    from marketmind_shared.schemas.trader import SignalKind

    closes = [100.0] * 20 + [105.0, 110.0, 115.0, 120.0, 125.0]
    df = make_candles(closes)
    position = _open_position_at(Decimal("105"), Decimal("90"))
    template = MaTrendTemplate(_params())
    result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.HOLD


def test_evaluate_is_deterministic(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """Same inputs ⇒ identical SignalEvaluation across calls.
    Determinism is the load-bearing property that makes paper
    results reproducible.
    """
    closes = [100.0] * 20 + [105.0, 110.0, 115.0, 120.0, 125.0]
    df = make_candles(closes)
    template = MaTrendTemplate(_params())
    first = template.evaluate(df, position=None)
    second = template.evaluate(df, position=None)
    assert first == second
