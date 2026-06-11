"""Tests for the bb_mean_reversion template.

Same rationale as the RSI mean-reversion tests: mocked indicators
because the realistic "close below lower band AND close > trend EMA"
fixture is hard to engineer (any drop big enough to breach the
Bollinger lower band typically also pulls close under the trend EMA,
which is exactly what the filter is designed to prevent).
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
from marketmind_workers.trader.templates.bb_mean_reversion import (
    BbMeanReversionParams,
    BbMeanReversionTemplate,
)
from pydantic import ValidationError


def _params() -> BbMeanReversionParams:
    return BbMeanReversionParams(
        bb_period=5,
        bb_std=2.0,
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


def _fake_bb(idx: pd.Index, lower: float, middle: float, upper: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lower": [lower] * len(idx),
            "middle": [middle] * len(idx),
            "upper": [upper] * len(idx),
        },
        index=idx,
    )


def test_params_reject_zero_bb_std() -> None:
    with pytest.raises(ValidationError):
        BbMeanReversionParams(bb_std=0.0)


def test_min_bars_needed_uses_longest_window() -> None:
    t = BbMeanReversionTemplate(_params())
    # max(bb=5, trend=5, atr=5) + 5 = 10
    assert t.min_bars_needed() == 10


def test_evaluate_buys_when_close_below_lower_and_trend_up(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    # Engineer: lower band = 105 > close (100), but trend (95) < close (100).
    fake_bb_df = _fake_bb(df.index, lower=105.0, middle=110.0, upper=115.0)
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = BbMeanReversionTemplate(_params())
    with (
        patch.object(ind, "bollinger", return_value=fake_bb_df),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=None)

    assert result.kind is SignalKind.BUY
    expected_stop = atr_stop_for_long(Decimal("100"), Decimal("5"), Decimal("2.0"))
    assert result.proposed_entry_price == Decimal("100")
    assert result.proposed_stop_price == expected_stop
    assert result.indicators == {
        "bb_lower": 105.0,
        "bb_middle": 110.0,
        "ema_trend": 95.0,
        "atr": 5.0,
    }


def test_evaluate_holds_when_below_lower_but_trend_down(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_bb_df = _fake_bb(df.index, lower=105.0, middle=110.0, upper=115.0)
    fake_trend = _const_series(120.0, df.index)  # trend above close ⇒ trend-down
    fake_atr = _const_series(5.0, df.index)

    template = BbMeanReversionTemplate(_params())
    with (
        patch.object(ind, "bollinger", return_value=fake_bb_df),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_holds_when_close_above_lower(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_bb_df = _fake_bb(df.index, lower=90.0, middle=95.0, upper=100.0)
    fake_trend = _const_series(85.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = BbMeanReversionTemplate(_params())
    with (
        patch.object(ind, "bollinger", return_value=fake_bb_df),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_exits_when_position_open_and_close_above_middle(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    fake_bb_df = _fake_bb(df.index, lower=80.0, middle=95.0, upper=110.0)
    fake_trend = _const_series(85.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = BbMeanReversionTemplate(_params())
    position = _open_position_at(Decimal("90"), Decimal("80"))
    with (
        patch.object(ind, "bollinger", return_value=fake_bb_df),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        result = template.evaluate(df, position=position)
    assert result.kind is SignalKind.EXIT
    assert result.proposed_stop_price == Decimal("80")


def test_evaluate_realistic_integration_holds_on_flat_series(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    from marketmind_shared.schemas.trader import SignalKind

    df = make_candles([100.0] * 20)
    template = BbMeanReversionTemplate(_params())
    result = template.evaluate(df, position=None)
    assert result.kind is SignalKind.HOLD


def test_evaluate_is_deterministic(
    make_candles: Callable[[list[float]], pd.DataFrame],
) -> None:
    df = make_candles([100.0] * 20)
    fake_bb_df = _fake_bb(df.index, lower=105.0, middle=110.0, upper=115.0)
    fake_trend = _const_series(95.0, df.index)
    fake_atr = _const_series(5.0, df.index)

    template = BbMeanReversionTemplate(_params())
    with (
        patch.object(ind, "bollinger", return_value=fake_bb_df),
        patch.object(ind, "ema", return_value=fake_trend),
        patch.object(ind, "atr", return_value=fake_atr),
    ):
        first = template.evaluate(df, position=None)
        second = template.evaluate(df, position=None)
    assert first == second
