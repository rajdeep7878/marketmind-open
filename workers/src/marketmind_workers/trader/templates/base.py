"""Base types for trader strategy templates.

A template is a deterministic class that converts a closed-candle
history plus an optional open position into a SignalEvaluation.
Same inputs ⇒ identical outputs, every call: no clock reads, no
I/O, no randomness. This is the load-bearing property that makes
paper results reproducible and lets the drift analyzer's
paper-vs-backtest comparison stay meaningful.

Construction protocol: every concrete template takes a typed Params
model. The signal engine builds one instance per
`(strategy_version, symbol)` at startup via
`build_template(name, raw_params)` (see `templates.__init__`) and
calls `evaluate(candles, position)` on each cycle.

Default stop policy: every BUY signal MUST carry a stop. v1
templates all default to an ATR-multiple long stop computed via
`atr_stop_for_long(entry, atr_value, multiple)` so the math is
centralised — diverging here would invalidate the backtest parity
the trader is anchored to.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import ClassVar

import pandas as pd
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.trader import (
    IndicatorSnapshot,
    PaperPosition,
    SignalEvaluation,
    SignalKind,
    TemplateName,
)
from marketmind_shared.trader.money import quantize_price


class TemplateParams(_StrictModel):
    """Base for every template's typed Params model.

    Inherits the _StrictModel guarantees (`frozen=True`,
    `extra='forbid'`, `validate_assignment=True`,
    `str_strip_whitespace=True`). Subclasses add their own fields
    plus cross-cutting `@model_validator`s (e.g. `fast < slow`).
    This empty class exists so the registry and downstream callers
    can refer to a single common base type.
    """


class StrategyTemplate(ABC):
    """Common interface every concrete template implements.

    Concrete templates must:
    - Set `template_name` as a ClassVar to a TemplateName member.
    - Implement `min_bars_needed()` based on their longest
      indicator window + a buffer for lookback math.
    - Implement `evaluate()` to produce a deterministic
      `SignalEvaluation`.

    `evaluate` must NEVER read the wall clock, call any I/O, or
    use a source of randomness. Determinism is verified by the
    snapshot tests in `workers/tests/test_trader_template_*.py`.
    """

    template_name: ClassVar[TemplateName]

    @abstractmethod
    def min_bars_needed(self) -> int:
        """Minimum closed candles required for a meaningful signal.

        The signal engine fetches at least this many rows from
        `trader_candles` before calling `evaluate()`. Templates
        size this against their longest indicator window — a 200-EMA
        needs at least 200 bars to even produce a non-NaN value.
        """

    @abstractmethod
    def evaluate(
        self,
        candles: pd.DataFrame,
        position: PaperPosition | None,
    ) -> SignalEvaluation:
        """Convert closed-candle history + optional position to a signal.

        `candles` is a DataFrame with:
          - tz-aware UTC DatetimeIndex of bar OPEN times (ascending,
            contiguous on the strategy's timeframe);
          - float64 columns: open, high, low, close, volume;
          - at least `min_bars_needed()` rows.

        `position` is the currently open `PaperPosition` for the
        `(strategy_version, symbol)` this template instance covers,
        or `None` if flat.

        Return is always a `SignalEvaluation`. HOLD evaluations
        are logged but never persisted; BUY / EXIT evaluations
        flow through risk + execution.
        """


def atr_stop_for_long(entry: Decimal, atr_value: Decimal, multiple: Decimal) -> Decimal:
    """Long-side ATR stop: ``entry - multiple * ATR``.

    Quantised at price precision. Centralised so every v1 template's
    default stop has identical numerics — diverging here would
    invalidate the backtest parity that approves each strategy.

    BACKTEST PARITY CONTRACT
    ------------------------
    MarketMind's backtest engine
    (``workers/backtest/engine.py::_vbt_stop_loss``) sets the stop
    via vbt as a *percent-of-fill-price*:

        sl_stop[t] = (ATR[t] * mult) / close[t]
        actual_stop_price = open[t+1] * (1 - sl_stop[t])

    where ``open[t+1]`` is the entry fill price.

    Trader returns ``close[t] - multiple * ATR[t]``, an *absolute*
    price computed at signal time. The two forms are identical iff
    ``close[t] == open[t+1]`` (no overnight gap). When they differ,
    Trader's stop is off by approximately::

        mult * ATR[t] * (open[t+1] / close[t] - 1)

    For 4h crypto candles this is typically < 0.01% of price — well
    within the drift analyzer's 30% tolerance (Step 9), so the
    divergence will NOT trigger false-positive "strategy decay"
    alerts. Sizing has the same shape: backtest achieves
    ``max_loss == risk_pct * equity`` exactly via vbt's
    percent-of-cash sizing; Trader achieves the same invariant
    exactly when ``close[t] == fill_price`` and approximately
    otherwise.

    The ATR series itself is BYTE-IDENTICAL across backtest and
    Trader: both call ``marketmind_workers.backtest.indicators.atr``
    with the same period. There is one indicator module instance,
    not two parallel implementations — this is the load-bearing
    parity guarantee.

    SEED-SCRIPT INVARIANT (Step 14)
    -------------------------------
    For a Trader version snapshotted from a MarketMind-approved
    backtest with ``StopLossAtrMultiple``, the seed script must
    propagate ``spec.exit.exits[StopLoss].method.atr_period`` and
    ``.mult`` into the Trader template's ``atr_period`` and
    ``atr_mult`` params. Otherwise a strategy backtested at
    ATR(20) would run on Trader at ATR(14) by template default —
    that mismatch IS real divergence the drift analyzer would
    (correctly) flag.
    """
    return quantize_price(entry - multiple * atr_value)


def hold(
    reason: str,
    indicators: IndicatorSnapshot,
    latest_close: Decimal,
) -> SignalEvaluation:
    """Factory for a HOLD evaluation.

    HOLD signals are logged but never persisted, so the stop / entry
    fields are placeholders. `proposed_entry_price` carries the
    latest close (informational for the audit log);
    `proposed_stop_price` is `Decimal(0)` — deliberately meaningless,
    so any misuse by a downstream caller surfaces immediately.
    """
    return SignalEvaluation(
        kind=SignalKind.HOLD,
        reason=reason,
        indicators=indicators,
        proposed_entry_price=latest_close,
        proposed_stop_price=Decimal(0),
    )


__all__ = [
    "StrategyTemplate",
    "TemplateParams",
    "atr_stop_for_long",
    "hold",
]
