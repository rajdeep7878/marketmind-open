"""rsi_mean_reversion strategy template — RSI oversold + trend filter.

Entry: RSI < oversold_threshold AND close > trend EMA.
       The trend filter is the load-bearing piece: buying oversold
       dips inside a structural downtrend ("catching a falling knife")
       is the textbook way mean-reversion strategies destroy capital.
Exit:  RSI > midline (typically 50).
Stop:  entry - atr_mult * ATR(atr_period).

Defaults: rsi=14, oversold=30, midline=50, trend=200, atr=14,
atr_mult=2.0.
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


class RsiMeanReversionParams(TemplateParams):
    rsi_period: int = Field(default=14, ge=2)
    oversold_threshold: float = Field(default=30.0, gt=0.0, lt=100.0)
    midline: float = Field(default=50.0, gt=0.0, lt=100.0)
    trend_ema_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_mult: Decimal = Field(default=Decimal("2.0"), gt=Decimal(0))

    @model_validator(mode="after")
    def _oversold_below_midline(self) -> Self:
        if self.oversold_threshold >= self.midline:
            raise PydanticCustomError(
                "oversold_must_be_below_midline",
                "oversold_threshold ({oversold}) must be < midline ({midline})",
                {"oversold": self.oversold_threshold, "midline": self.midline},
            )
        return self


class RsiMeanReversionTemplate(StrategyTemplate):
    template_name: ClassVar[TemplateName] = TemplateName.RSI_MEAN_REVERSION

    def __init__(self, params: RsiMeanReversionParams) -> None:
        self.params = params

    def min_bars_needed(self) -> int:
        return (
            max(
                self.params.rsi_period,
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
        rsi = ind.rsi(candles, self.params.rsi_period)
        trend_ema = ind.ema(candles, self.params.trend_ema_period)
        atr = ind.atr(candles, self.params.atr_period)

        close_now = float(candles["close"].iloc[-1])
        rsi_now = float(rsi.iloc[-1])
        trend_now = float(trend_ema.iloc[-1])
        atr_now = float(atr.iloc[-1])

        if any(pd.isna(v) for v in [rsi_now, trend_now, atr_now]):
            return hold("indicators not warm yet", {}, to_decimal(close_now))

        snapshot = {
            "rsi": rsi_now,
            "ema_trend": trend_now,
            "atr": atr_now,
        }

        if position is None:
            oversold = rsi_now < self.params.oversold_threshold
            trend_up = close_now > trend_now
            if oversold and trend_up:
                entry = to_decimal(close_now)
                atr_d = to_decimal(atr_now)
                stop = atr_stop_for_long(entry, atr_d, self.params.atr_mult)
                return SignalEvaluation(
                    kind=SignalKind.BUY,
                    reason="RSI oversold + close above trend EMA",
                    indicators=snapshot,
                    proposed_entry_price=entry,
                    proposed_stop_price=stop,
                )
            return hold(
                "not oversold or trend filter false",
                snapshot,
                to_decimal(close_now),
            )

        # Position open: exit when RSI recovers past the midline.
        if rsi_now > self.params.midline:
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason="RSI rose above midline — mean reversion target reached",
                indicators=snapshot,
                proposed_entry_price=to_decimal(close_now),
                proposed_stop_price=position.stop_price,
            )
        return hold(
            "position open, RSI still below midline",
            snapshot,
            to_decimal(close_now),
        )


__all__ = ["RsiMeanReversionParams", "RsiMeanReversionTemplate"]
