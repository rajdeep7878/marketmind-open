"""breakout strategy template — Donchian channel breakout with trend filter.

Entry: close > N-period high (computed over the prior N bars, NOT
       including the current bar) AND close > trend EMA.
Exit:  close < M-period low (prior M bars).
Stop:  entry - atr_mult * ATR(atr_period).

Defaults: breakout=20, exit=10, trend=200, atr=14, atr_mult=2.0.
Classic Donchian "channel breakout" with an EMA trend filter to
avoid buying breakouts inside structural downtrends.
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


class BreakoutParams(TemplateParams):
    breakout_period: int = Field(default=20, ge=2)
    exit_period: int = Field(default=10, ge=2)
    trend_ema_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_mult: Decimal = Field(default=Decimal("2.0"), gt=Decimal(0))


class BreakoutTemplate(StrategyTemplate):
    template_name: ClassVar[TemplateName] = TemplateName.BREAKOUT

    def __init__(self, params: BreakoutParams) -> None:
        self.params = params

    def min_bars_needed(self) -> int:
        return (
            max(
                self.params.breakout_period,
                self.params.exit_period,
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
        # ind.highest / ind.lowest return rolling max/min INCLUDING
        # the current bar. shift(1) gives us "prior N bars only" so
        # the current close isn't compared against its own high.
        prior_high = ind.highest(candles, self.params.breakout_period).shift(1)
        prior_low = ind.lowest(candles, self.params.exit_period).shift(1)
        trend_ema = ind.ema(candles, self.params.trend_ema_period)
        atr = ind.atr(candles, self.params.atr_period)

        close_now = float(candles["close"].iloc[-1])
        prior_high_now = float(prior_high.iloc[-1])
        prior_low_now = float(prior_low.iloc[-1])
        trend_now = float(trend_ema.iloc[-1])
        atr_now = float(atr.iloc[-1])

        warmup_values = [prior_high_now, prior_low_now, trend_now, atr_now]
        if any(pd.isna(v) for v in warmup_values):
            return hold("indicators not warm yet", {}, to_decimal(close_now))

        snapshot = {
            "prior_high": prior_high_now,
            "prior_low": prior_low_now,
            "ema_trend": trend_now,
            "atr": atr_now,
        }

        if position is None:
            broke_high = close_now > prior_high_now
            trend_up = close_now > trend_now
            if broke_high and trend_up:
                entry = to_decimal(close_now)
                atr_d = to_decimal(atr_now)
                stop = atr_stop_for_long(entry, atr_d, self.params.atr_mult)
                return SignalEvaluation(
                    kind=SignalKind.BUY,
                    reason="close broke above prior-N high + trend up",
                    indicators=snapshot,
                    proposed_entry_price=entry,
                    proposed_stop_price=stop,
                )
            return hold(
                "no breakout or trend filter false",
                snapshot,
                to_decimal(close_now),
            )

        # Position open: exit on close below prior-M low.
        if close_now < prior_low_now:
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason="close fell below prior-M low",
                indicators=snapshot,
                proposed_entry_price=to_decimal(close_now),
                proposed_stop_price=position.stop_price,
            )
        return hold(
            "position open, no exit signal",
            snapshot,
            to_decimal(close_now),
        )


__all__ = ["BreakoutParams", "BreakoutTemplate"]
