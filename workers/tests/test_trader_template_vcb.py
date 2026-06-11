"""Tests for the vcb (volatility contraction breakout) template.

Lower-priority template in v1 — tests focus on the dispatch logic and
the contraction-ratio + breakout combination. ATR is mocked via a
period-aware side_effect so the short / long / stop ATR calls each
return distinct values.
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
from marketmind_workers.trader.templates.vcb import VcbParams, VcbTemplate
from pydantic import ValidationError


def _params() -> VcbParams:
    return VcbParams(
        short_atr_period=3,
        long_atr_period=10,
        contraction_threshold=0.7,
        breakout_period=5,
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


def _atr_router(
    idx: pd.Index,
    short_val: float,
    long_val: float,
    stop_val: float,
):  # type: ignore[no-untyped-def]
    """Build a fake `ind.atr` whose return depends on the period
    argument so the template's three ATR calls — short, long, and
    stop — each get their own value.
    """
    short_p = 3
    long_p = 10
    stop_p = 5
    mapping: dict[int, pd.Series] = {
        short_p: _const_series(short_val, idx),
        long_p: _const_series(long_val, idx),
        stop_p: _const_series(stop_val, idx),
    }

    def _fake(_df: pd.DataFrame, period: int) -> pd.Series:
        return mapping[period]

    return _fake


def test_params_reject_short_geq_long_atr() -> None:
    with pytest.raises(ValidationError, match="short_atr_must_be_less_than_long"):
        VcbParams(short_atr_period=10, long_atr_period=10)


def test_min_bars_needed_uses_longest_window() -> None:
    t = VcbTemplate(_params())
    # max(long_atr=10, breakout=5, trend=5, atr=5) + 5 = 15
    assert t.min_bars_needed() == 15


def test_evaluate_buys_on_compression_breakout_with_trend_up(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    # Compression: short_atr=1, long_atr=2 ⇒ ratio 0.5 < threshold 0.7.
    fake_atr = _atr_router(df.index, short_val=1.0, long_val=2.0, stop_val=5.0)
    # Prior high = 99 ⇒ close (100) breaks it.
    fake_high = _const_series(99.0, df.index)
    fake_low = _const_series(95.0, df.index)
    # Trend below close ⇒ trend filter passes.
    fake_trend = _const_series(95.0, df.index)

    template = VcbTemplate(_params())
    with (
        patch.object(ind, "atr", side_effect=fake_atr),
        patch.object(ind, "highest", return_value=fake_high),
        patch.object(ind, "lowest", return_value=fake_low),
        patch.object(ind, "ema", return_value=fake_trend),
    ):
        result = template.evaluate(df, position=None)

    assert result.kind is SignalKind.BUY
    expected_stop = atr_stop_for_long(Decimal("100"), Decimal("5"), Decimal("2.0"))
    assert result.proposed_entry_price == Decimal("100")
    assert result.proposed_stop_price == expected_stop
    assert result.indicators["compression_ratio"] == pytest.approx(0.5)


def test_evaluate_holds_without_compression(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    # No compression: ratio = 2/2 = 1.0 (above threshold).
    fake_atr = _atr_router(df.index, short_val=2.0, long_val=2.0, stop_val=5.0)
    fake_high = _const_series(99.0, df.index)
    fake_low = _const_series(95.0, df.index)
    fake_trend = _const_series(95.0, df.index)

    template = VcbTemplate(_params())
    with (
        patch.object(ind, "atr", side_effect=fake_atr),
        patch.object(ind, "highest", return_value=fake_high),
        patch.object(ind, "lowest", return_value=fake_low),
        patch.object(ind, "ema", return_value=fake_trend),
    ):
        result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_holds_when_compression_but_no_breakout(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_atr = _atr_router(df.index, short_val=1.0, long_val=2.0, stop_val=5.0)
    # Prior high = 110 — close (100) doesn't break it.
    fake_high = _const_series(110.0, df.index)
    fake_low = _const_series(95.0, df.index)
    fake_trend = _const_series(95.0, df.index)

    template = VcbTemplate(_params())
    with (
        patch.object(ind, "atr", side_effect=fake_atr),
        patch.object(ind, "highest", return_value=fake_high),
        patch.object(ind, "lowest", return_value=fake_low),
        patch.object(ind, "ema", return_value=fake_trend),
    ):
        result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_exits_on_failed_breakout(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_atr = _atr_router(df.index, short_val=1.0, long_val=2.0, stop_val=5.0)
    # Prior low = 105 — close (100) falls below ⇒ EXIT.
    fake_high = _const_series(99.0, df.index)
    fake_low = _const_series(105.0, df.index)
    fake_trend = _const_series(95.0, df.index)

    template = VcbTemplate(_params())
    position = _open_position_at(Decimal("99"), Decimal("90"))
    with (
        patch.object(ind, "atr", side_effect=fake_atr),
        patch.object(ind, "highest", return_value=fake_high),
        patch.object(ind, "lowest", return_value=fake_low),
        patch.object(ind, "ema", return_value=fake_trend),
    ):
        result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.EXIT
    assert result.proposed_stop_price == Decimal("90")


def test_evaluate_realistic_integration_holds_on_flat_series(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    """End-to-end with real indicators on a flat series: no
    compression / breakout pattern ⇒ HOLD. Smoke-tests the wiring.
    """
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    template = VcbTemplate(_params())
    result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD
