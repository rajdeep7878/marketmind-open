"""Tests for the rsi_mean_reversion template.

Mocks the three indicator functions (`ind.rsi`, `ind.ema`, `ind.atr`)
rather than synthesising an oversold-but-uptrending price series. The
"realistic" series is hard to construct because by the time RSI dips
below 30, close has usually crossed below the recent trend EMA — the
template's trend filter is precisely designed to keep you out of that
trade. Mocking the indicators isolates the template's branching logic
from the indicator math (which has its own coverage in
`test_indicators.py`). One realistic-integration test wires the
indicator module end-to-end on a flat series.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
import pytest
from marketmind_workers.backtest import indicators as ind
from marketmind_workers.trader.templates.base import atr_stop_for_long
from marketmind_workers.trader.templates.rsi_mean_reversion import (
    RsiMeanReversionParams,
    RsiMeanReversionTemplate,
)
from pydantic import ValidationError


def _params() -> RsiMeanReversionParams:
    return RsiMeanReversionParams(
        rsi_period=4,
        oversold_threshold=30.0,
        midline=50.0,
        trend_ema_period=5,
        atr_period=5,
        atr_mult=Decimal("2.0"),
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


def _const_series(value: float, idx: pd.Index) -> pd.Series:
    return pd.Series([value] * len(idx), index=idx)


def test_params_reject_oversold_geq_midline() -> None:
    with pytest.raises(ValidationError, match="oversold_must_be_below_midline"):
        RsiMeanReversionParams(oversold_threshold=60.0, midline=50.0)


def test_min_bars_needed_uses_longest_window() -> None:
    t = RsiMeanReversionTemplate(_params())
    # max(rsi=4, trend=5, atr=5) + 5 = 10
    assert t.min_bars_needed() == 10


def test_evaluate_buys_on_oversold_and_trend_up(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    # RSI = 25 (oversold), trend EMA = 95 (close > trend), ATR = 5.
    fake_rsi = _const_series(25.0, df.index)
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = RsiMeanReversionTemplate(_params())
    with (
        patch.object(ind, "rsi", return_value=fake_rsi),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=None)

    assert result.kind is SignalKind.BUY
    expected_stop = atr_stop_for_long(Decimal("100"), Decimal("5"), Decimal("2.0"))
    assert result.proposed_entry_price == Decimal("100")
    assert result.proposed_stop_price == expected_stop
    assert result.indicators == {"rsi": 25.0, "ema_trend": 95.0, "atr": 5.0}


def test_evaluate_holds_when_oversold_but_trend_down(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """The trend filter is load-bearing: oversold + trend down ⇒ HOLD.
    This is the "no falling-knife catching" guarantee.
    """
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_rsi = _const_series(20.0, df.index)
    fake_trend = _const_series(110.0, df.index)  # close (100) < trend (110)
    fake_atr = _const_series(5.0, df.index)

    template = RsiMeanReversionTemplate(_params())
    with (
        patch.object(ind, "rsi", return_value=fake_rsi),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_holds_when_not_oversold(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_rsi = _const_series(45.0, df.index)  # above oversold but below midline
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = RsiMeanReversionTemplate(_params())
    with (
        patch.object(ind, "rsi", return_value=fake_rsi),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_exits_when_position_open_and_rsi_above_midline(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_rsi = _const_series(60.0, df.index)  # above midline (50)
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = RsiMeanReversionTemplate(_params())
    position = _open_position_at(Decimal("95"), Decimal("90"))
    with (
        patch.object(ind, "rsi", return_value=fake_rsi),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.EXIT
    assert result.proposed_stop_price == Decimal("90")


def test_evaluate_holds_when_position_open_and_rsi_below_midline(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_rsi = _const_series(40.0, df.index)
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = RsiMeanReversionTemplate(_params())
    position = _open_position_at(Decimal("95"), Decimal("90"))
    with (
        patch.object(ind, "rsi", return_value=fake_rsi),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.HOLD


def test_evaluate_realistic_integration_holds_on_flat_series(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """End-to-end with real indicators on a flat series: RSI is ~50,
    not oversold ⇒ HOLD. Verifies the indicator wiring is correct
    without depending on a hard-to-construct oversold-uptrend fixture.
    """
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    template = RsiMeanReversionTemplate(_params())
    result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_is_deterministic(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    df = make_candles([100.0] * 20)
    fake_rsi = _const_series(25.0, df.index)
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = RsiMeanReversionTemplate(_params())
    with (
        patch.object(ind, "rsi", return_value=fake_rsi),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        first = template.evaluate(df, position=None)
        second = template.evaluate(df, position=None)
    assert first == second
