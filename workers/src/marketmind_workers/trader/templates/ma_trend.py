"""ma_trend strategy template — EMA crossover with trend filter.

Entry: fast EMA crosses ABOVE slow EMA AND close > trend EMA.
Exit:  fast EMA crosses BELOW slow EMA (opposite cross).
Stop:  entry - atr_mult * ATR(atr_period).

Defaults track the classic MACD pair (fast=12, slow=26) plus a
200-period trend filter that keeps the strategy out of structural
downtrends.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Self

import pandas as pd
from marketmind_shared.schemas.trader import (
    PaperPosition,
    SignalEvaluation,
    SignalKind,
    TemplateName,
)
from marketmind_shared.trader.money import to_decimal
from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_workers.backtest import indicators as ind
from marketmind_workers.trader.templates.base import (
    StrategyTemplate,
    TemplateParams,
    atr_stop_for_long,
    hold,
)


class MaTrendParams(TemplateParams):
    fast_ema_period: int = Field(default=12, ge=2)
    slow_ema_period: int = Field(default=26, ge=2)
    trend_ema_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_mult: Decimal = Field(default=Decimal("2.0"), gt=Decimal(0))

    @model_validator(mode="after")
    def _fast_less_than_slow(self) -> Self:
        if self.fast_ema_period >= self.slow_ema_period:
            raise PydanticCustomError(
                "fast_must_be_less_than_slow",
                "fast_ema_period ({fast}) must be < slow_ema_period ({slow})",
                {"fast": self.fast_ema_period, "slow": self.slow_ema_period},
            )
        return self


class MaTrendTemplate(StrategyTemplate):
    template_name: ClassVar[TemplateName] = TemplateName.MA_TREND

    def __init__(self, params: MaTrendParams) -> None:
        self.params = params

    def min_bars_needed(self) -> int:
        # Trend EMA is usually the longest; +5 buffer warms the
        # series and gives us a prev bar for crossover detection.
        return (
            max(
                self.params.slow_ema_period,
                self.params.trend_ema_period,
                self.params.atr_period,
            )
            + 5
        )

    def evaluate(
        self,
        candles: pd.DataFrame,
        position: PaperPosition | None,
    ) -> SignalEvaluation:
        fast_ema = ind.ema(candles, self.params.fast_ema_period)
        slow_ema = ind.ema(candles, self.params.slow_ema_period)
        trend_ema = ind.ema(candles, self.params.trend_ema_period)
        atr = ind.atr(candles, self.params.atr_period)

        close_now = float(candles["close"].iloc[-1])

        if len(candles) < 2:
            return hold("insufficient history", {}, to_decimal(close_now))

        fast_prev = float(fast_ema.iloc[-2])
        fast_now = float(fast_ema.iloc[-1])
        slow_prev = float(slow_ema.iloc[-2])
        slow_now = float(slow_ema.iloc[-1])
        trend_now = float(trend_ema.iloc[-1])
        atr_now = float(atr.iloc[-1])

        # Indicators are NaN until their warmup completes.
        # min_bars_needed() should prevent this, but a defensive
        # HOLD is cheaper than a malformed signal.
        warmup_values = [fast_prev, fast_now, slow_prev, slow_now, trend_now, atr_now]
        if any(pd.isna(v) for v in warmup_values):
            return hold("indicators not warm yet", {}, to_decimal(close_now))

        snapshot = {
            "ema_fast": fast_now,
            "ema_slow": slow_now,
            "ema_trend": trend_now,
            "atr": atr_now,
        }

        crossed_above = fast_prev <= slow_prev and fast_now > slow_now
        crossed_below = fast_prev >= slow_prev and fast_now < slow_now

        if position is None:
            trend_up = close_now > trend_now
            if crossed_above and trend_up:
                entry = to_decimal(close_now)
                atr_d = to_decimal(atr_now)
                stop = atr_stop_for_long(entry, atr_d, self.params.atr_mult)
                return SignalEvaluation(
                    kind=SignalKind.BUY,
                    reason="fast EMA crossed above slow + close above trend EMA",
                    indicators=snapshot,
                    proposed_entry_price=entry,
                    proposed_stop_price=stop,
                )
            return hold(
                "no upward cross or trend filter false",
                snapshot,
                to_decimal(close_now),
            )

        # Position open: exit on opposite cross.
        if crossed_below:
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason="fast EMA crossed below slow — opposite cross",
                indicators=snapshot,
                proposed_entry_price=to_decimal(close_now),
                # Carry the position's stop forward; the EXIT signal
                # is closing the position so this value is purely
                # informational for the audit trail.
                proposed_stop_price=position.stop_price,
            )
        return hold(
            "position open, no opposite cross",
            snapshot,
            to_decimal(close_now),
        )


__all__ = ["MaTrendParams", "MaTrendTemplate"]
