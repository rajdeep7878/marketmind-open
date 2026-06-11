"""bb_mean_reversion strategy template — Bollinger lower-band mean reversion.

Entry: close < lower Bollinger band AND close > trend EMA.
       Same logic as RSI mean reversion's trend filter — buy extreme
       dips only when the longer-term trend is up.
Exit:  close > middle Bollinger band (SMA of the bb_period window).
Stop:  entry - atr_mult * ATR(atr_period).

Defaults: bb_period=20, bb_std=2.0, trend=200, atr=14, atr_mult=2.0.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import pandas as pd
from marketmind_shared.schemas.trader import (
    PaperPosition,
    SignalEvaluation,
    SignalKind,
    TemplateName,
)
from marketmind_shared.trader.money import to_decimal
from pydantic import Field

from marketmind_workers.backtest import indicators as ind
from marketmind_workers.trader.templates.base import (
    StrategyTemplate,
    TemplateParams,
    atr_stop_for_long,
    hold,
)


class BbMeanReversionParams(TemplateParams):
    bb_period: int = Field(default=20, ge=2)
    bb_std: float = Field(default=2.0, gt=0.0)
    trend_ema_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_mult: Decimal = Field(default=Decimal("2.0"), gt=Decimal(0))


class BbMeanReversionTemplate(StrategyTemplate):
    template_name: ClassVar[TemplateName] = TemplateName.BB_MEAN_REVERSION

    def __init__(self, params: BbMeanReversionParams) -> None:
        self.params = params

    def min_bars_needed(self) -> int:
        return (
            max(
                self.params.bb_period,
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
        bb = ind.bollinger(candles, self.params.bb_period, self.params.bb_std)
        trend_ema = ind.ema(candles, self.params.trend_ema_period)
        atr = ind.atr(candles, self.params.atr_period)

        close_now = float(candles["close"].iloc[-1])
        lower_now = float(bb["lower"].iloc[-1])
        middle_now = float(bb["middle"].iloc[-1])
        trend_now = float(trend_ema.iloc[-1])
        atr_now = float(atr.iloc[-1])

        if any(pd.isna(v) for v in [lower_now, middle_now, trend_now, atr_now]):
            return hold("indicators not warm yet", {}, to_decimal(close_now))

        snapshot = {
            "bb_lower": lower_now,
            "bb_middle": middle_now,
            "ema_trend": trend_now,
            "atr": atr_now,
        }

        if position is None:
            below_lower = close_now < lower_now
            trend_up = close_now > trend_now
            if below_lower and trend_up:
                entry = to_decimal(close_now)
                atr_d = to_decimal(atr_now)
                stop = atr_stop_for_long(entry, atr_d, self.params.atr_mult)
                return SignalEvaluation(
                    kind=SignalKind.BUY,
                    reason="close below lower band + trend up",
                    indicators=snapshot,
                    proposed_entry_price=entry,
                    proposed_stop_price=stop,
                )
            return hold(
                "not below lower band or trend filter false",
                snapshot,
                to_decimal(close_now),
            )

        # Position open: exit when close crosses above the middle.
        if close_now > middle_now:
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason="close above middle band — mean reverted",
                indicators=snapshot,
                proposed_entry_price=to_decimal(close_now),
                proposed_stop_price=position.stop_price,
            )
        return hold(
            "position open, close still below middle",
            snapshot,
            to_decimal(close_now),
        )


__all__ = ["BbMeanReversionParams", "BbMeanReversionTemplate"]
