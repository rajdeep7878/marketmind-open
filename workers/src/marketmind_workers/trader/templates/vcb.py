"""vcb (volatility contraction breakout) strategy template.

Detects a volatility-compression regime via the ratio of short-window
ATR to long-window ATR, then enters on a breakout above the
prior-N high. Trend filter avoids buying breakouts in downtrends.

Lower-priority template in v1 — ships with a simple ATR-ratio
contraction detector. The literature offers more sophisticated
forms (Mark Minervini's "VCP" uses multiple successive
contractions); v2 can replace the detector without changing the
public signal interface.

Entry: ATR_short / ATR_long < contraction_threshold (compressed)
       AND close > prior-N high
       AND close > trend EMA.
Exit:  close < prior-N low (failed breakout) OR ATR stop.
Stop:  entry - atr_mult * ATR(atr_period).
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


class VcbParams(TemplateParams):
    short_atr_period: int = Field(default=5, ge=2)
    long_atr_period: int = Field(default=20, ge=2)
    # Ratio threshold: short ATR must drop below this fraction of
    # long ATR before we consider the regime "compressed". 0.7
    # is roughly 30% volatility compression — a meaningful pause.
    contraction_threshold: float = Field(default=0.7, gt=0.0, le=1.0)
    breakout_period: int = Field(default=20, ge=2)
    trend_ema_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_mult: Decimal = Field(default=Decimal("2.0"), gt=Decimal(0))

    @model_validator(mode="after")
    def _short_less_than_long(self) -> Self:
        if self.short_atr_period >= self.long_atr_period:
            raise PydanticCustomError(
                "short_atr_must_be_less_than_long",
                "short_atr_period ({short}) must be < long_atr_period ({long})",
                {"short": self.short_atr_period, "long": self.long_atr_period},
            )
        return self


class VcbTemplate(StrategyTemplate):
    template_name: ClassVar[TemplateName] = TemplateName.VCB

    def __init__(self, params: VcbParams) -> None:
        self.params = params

    def min_bars_needed(self) -> int:
        return (
            max(
                self.params.long_atr_period,
                self.params.breakout_period,
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
        atr_short = ind.atr(candles, self.params.short_atr_period)
        atr_long = ind.atr(candles, self.params.long_atr_period)
        prior_high = ind.highest(candles, self.params.breakout_period).shift(1)
        prior_low = ind.lowest(candles, self.params.breakout_period).shift(1)
        trend_ema = ind.ema(candles, self.params.trend_ema_period)
        atr_stop = ind.atr(candles, self.params.atr_period)

        close_now = float(candles["close"].iloc[-1])
        atr_s_now = float(atr_short.iloc[-1])
        atr_l_now = float(atr_long.iloc[-1])
        prior_high_now = float(prior_high.iloc[-1])
        prior_low_now = float(prior_low.iloc[-1])
        trend_now = float(trend_ema.iloc[-1])
        atr_now = float(atr_stop.iloc[-1])

        # Defensive guards: NaN during warmup, zero long-ATR would
        # divide by zero on the contraction ratio.
        warmup_values = [
            atr_s_now,
            atr_l_now,
            prior_high_now,
            prior_low_now,
            trend_now,
            atr_now,
        ]
        if any(pd.isna(v) for v in warmup_values) or atr_l_now <= 0:
            return hold("indicators not warm yet", {}, to_decimal(close_now))

        compression_ratio = atr_s_now / atr_l_now

        snapshot = {
            "atr_short": atr_s_now,
            "atr_long": atr_l_now,
            "compression_ratio": compression_ratio,
            "prior_high": prior_high_now,
            "prior_low": prior_low_now,
            "ema_trend": trend_now,
            "atr": atr_now,
        }

        if position is None:
            compressed = compression_ratio < self.params.contraction_threshold
            broke_high = close_now > prior_high_now
            trend_up = close_now > trend_now
            if compressed and broke_high and trend_up:
                entry = to_decimal(close_now)
                atr_d = to_decimal(atr_now)
                stop = atr_stop_for_long(entry, atr_d, self.params.atr_mult)
                return SignalEvaluation(
                    kind=SignalKind.BUY,
                    reason="ATR contraction + breakout above prior-N high + trend up",
                    indicators=snapshot,
                    proposed_entry_price=entry,
                    proposed_stop_price=stop,
                )
            return hold(
                "no compressed breakout or trend filter false",
                snapshot,
                to_decimal(close_now),
            )

        # Position open: exit on close below prior-N low.
        if close_now < prior_low_now:
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason="close fell below prior-N low — failed breakout",
                indicators=snapshot,
                proposed_entry_price=to_decimal(close_now),
                proposed_stop_price=position.stop_price,
            )
        return hold(
            "position open, no breakout failure",
            snapshot,
            to_decimal(close_now),
        )


__all__ = ["VcbParams", "VcbTemplate"]
