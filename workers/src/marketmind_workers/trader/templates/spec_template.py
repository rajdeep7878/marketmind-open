"""Generic spec-executor template — `TemplateName.SPEC` (A.5a).

Unlike the five hand-coded v1 templates, `SpecTemplate` carries an
extracted v2 `StrategySpec` and evaluates it by **reusing the backtest
engine's condition evaluators** — it calls `translator.build_signals`,
the exact code the backtest runs. There is therefore one implementation
of condition evaluation, shared by the backtest and the live trader
(design doc §6A.0, resolved question Q1). A hand-coded transcription
would be a second evaluator to keep byte-identical by hand — the
divergence §6.6 forbids.

Scope: Tier-1 (bounded-window), Tier-2 (`regime_state`, `ratchet
reset="never"`), and — since A.6 — Tier-3 (`prior_trade`, `prior_signal`,
`ratchet reset="per_trade"`). Short specs, multi-timeframe specs, and
stopless specs are rejected at construction (see
`spec_template_rejection_reason`).

`evaluate` is **stateless** — it re-derives Tier-2 state from the candle
window each call. `evaluate_stateful` seeds from persisted state: for a
Tier-2 spec via `build_signals_stateful` (A.5b — a regime latch is
full-history-exact rather than window-truncated); for a Tier-3 spec via
the `iterative_live` shadow-simulation stepper (A.6, design doc §6C). The
signal engine routes stateful specs to `evaluate_stateful`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar, Self

import pandas as pd
from marketmind_shared.schemas.strategy_spec import (
    Direction,
    StopLossAtrMultiple,
    StopLossExit,
    StopLossMethod,
    StopLossPercent,
    StopLossTrailingAtr,
    StopLossTrailingPercent,
    StrategySpec,
    TakeProfitAtrMultiple,
    TakeProfitExit,
    TakeProfitFixedPrice,
    TakeProfitMethod,
    TakeProfitPercent,
    spec_uses_stateful_v2,
    spec_uses_tier3,
)
from marketmind_shared.schemas.trader import (
    IndicatorSnapshot,
    PaperPosition,
    SignalEvaluation,
    SignalKind,
    StrategyState,
    TemplateName,
)
from marketmind_shared.trader.money import quantize_price, to_decimal
from pydantic import model_validator
from pydantic_core import PydanticCustomError

from marketmind_workers.backtest import indicators as ind
from marketmind_workers.backtest.iterative_live import run_live_cycle
from marketmind_workers.backtest.translator import (
    SignalSet,
    _estimate_warmup_bars,  # pyright: ignore[reportPrivateUsage]
    build_signals,
    build_signals_stateful,
)
from marketmind_workers.trader.templates.base import (
    StrategyTemplate,
    TemplateParams,
    atr_stop_for_long,
    hold,
)

# A SpecTemplate loads a window this many times the indicator warmup.
# A.5b's persisted state seeds the regime latch / ratchet extremum, but
# the indicators *inside* a regime's enter/exit triggers are recursive
# (EMA, RSI, ATR): their value at the window's last bar still carries
# exponentially-decaying memory of pre-window bars. A ~5x-warmup window
# drives that truncation below ~0.1% (`(1-2/p)^(4p) ≈ e^-8`), so the
# live windowed evaluation matches the backtest's full-history one — the
# §6.6 / §6B.3 drift-parity property. The cost is loading a few hundred
# extra candles per cycle.
_WARMUP_WINDOW_MULTIPLE: int = 5
# Floor for specs with little or no indicator warmup.
_MIN_WINDOW_BARS: int = 200


def spec_template_rejection_reason(spec: StrategySpec) -> str | None:
    """Why `spec` cannot run as a `SpecTemplate`, or None if it can.

    Used both by `SpecParams` (to fail fast at `build_template` time) and
    by the seed script (to refuse a non-runnable spec at seed time). Tier-3
    specs are accepted since A.6 — a malformed Tier-3 *shape* the iterative
    evaluators reject is caught at runtime by the signal engine's
    disable-and-alert guard (§6A.3), not here.
    """
    if spec.direction is not Direction.LONG:
        return (
            f"SpecTemplate is long-only; this spec's direction is "
            f"'{spec.direction.value}'"
        )
    if spec.filter_timeframe is not None:
        return (
            "SpecTemplate does not support multi-timeframe specs; this "
            f"spec sets filter_timeframe='{spec.filter_timeframe.value}'"
        )
    if not any(isinstance(e, StopLossExit) for e in spec.exit.exits):
        return (
            "SpecTemplate requires the spec to define a stop_loss exit — the "
            "trader requires every entry to carry a protective stop"
        )
    return None


class SpecParams(TemplateParams):
    """Parameters for `SpecTemplate` — the extracted strategy spec itself.

    The DB `trader_strategy_versions.parameters` JSONB for a `spec`
    version is `{"spec": <StrategySpec JSON>}`. The strict base rejects
    any other key.
    """

    spec: StrategySpec

    @model_validator(mode="after")
    def _spec_is_a5a_runnable(self) -> Self:
        reason = spec_template_rejection_reason(self.spec)
        if reason is not None:
            raise PydanticCustomError(
                "spec_template_unsupported",
                "{reason}",
                {"reason": reason},
            )
        return self


class SpecTemplate(StrategyTemplate):
    """Runs a v2 `StrategySpec` through the shared backtest evaluators."""

    template_name: ClassVar[TemplateName] = TemplateName.SPEC

    def __init__(self, params: SpecParams) -> None:
        self.params = params
        self._spec = params.spec

    def min_bars_needed(self) -> int:
        return max(
            _estimate_warmup_bars(self._spec) * _WARMUP_WINDOW_MULTIPLE,
            _MIN_WINDOW_BARS,
        )

    @property
    def is_stateful(self) -> bool:
        """True when the spec uses a stateful (Tier-2 or Tier-3) condition.
        The signal engine routes such versions through `evaluate_stateful`
        and persists their `trader_strategy_state` (A.5b / A.6).
        """
        return spec_uses_stateful_v2(self._spec)

    @property
    def is_tier3(self) -> bool:
        """True when the spec uses a Tier-3 condition (prior_trade /
        prior_signal / per-trade ratchet). The signal engine loads the
        full candle history for such versions — the live shadow
        simulation's bar indices are absolute (design doc §6C).
        """
        return spec_uses_tier3(self._spec)

    def evaluate(
        self,
        candles: pd.DataFrame,
        position: PaperPosition | None,
    ) -> SignalEvaluation:
        # Stateless path: every Tier-2 recurrence re-derives from this
        # window. Used for non-stateful specs; a stateful spec goes
        # through evaluate_stateful so its regime latch reflects full
        # history rather than the truncated window.
        signal_set = build_signals(self._spec, {self._spec.primary_timeframe: candles})
        return self._decide(signal_set, candles, position)

    def evaluate_stateful(
        self,
        candles: pd.DataFrame,
        position: PaperPosition | None,
        prior_state: StrategyState | None,
    ) -> tuple[SignalEvaluation, StrategyState]:
        """Evaluate one cycle seeded from `prior_state` — the persisted
        state as of the previous evaluated candle — returning the decision
        and the state advanced to this candle. A cold start passes
        `prior_state=None`.

        A Tier-3 spec routes through the `iterative_live` shadow-simulation
        stepper (A.6, §6C); a Tier-2 spec through the vectorised
        `build_signals_stateful` seed (A.5b, §6B).
        """
        if self.is_tier3:
            return self._evaluate_tier3(candles, position, prior_state)
        signal_set, next_state = build_signals_stateful(
            self._spec,
            {self._spec.primary_timeframe: candles},
            prior_state,
        )
        return self._decide(signal_set, candles, position), next_state

    def _evaluate_tier3(
        self,
        candles: pd.DataFrame,
        position: PaperPosition | None,
        prior_state: StrategyState | None,
    ) -> tuple[SignalEvaluation, StrategyState]:
        """Advance the live Tier-3 shadow simulation by the new candle(s)
        via the `iterative_live` stepper (design doc §6C). `candles` is the
        full history — Tier-3 bar indices are absolute.
        """
        prior_tier3 = prior_state.tier3 if prior_state is not None else None
        last_bar = prior_tier3.last_bar if prior_tier3 is not None else -1
        next_tier3, kind = run_live_cycle(
            self._spec,
            {self._spec.primary_timeframe: candles},
            prior_tier3,
            last_bar,
        )
        return (
            self._decide_from_kind(kind, candles, position),
            StrategyState(tier3=next_tier3),
        )

    def _decide(
        self,
        signal_set: SignalSet,
        candles: pd.DataFrame,
        position: PaperPosition | None,
    ) -> SignalEvaluation:
        # The latest closed bar's entry/exit booleans are the live
        # decision — shared by evaluate (stateless) and evaluate_stateful.
        entered = bool(signal_set.entries.iloc[-1])
        exited = bool(signal_set.exits.iloc[-1])
        latest_close = to_decimal(float(candles["close"].iloc[-1]))
        snapshot: IndicatorSnapshot = {
            "entry_signal": 1.0 if entered else 0.0,
            "exit_signal": 1.0 if exited else 0.0,
        }

        if position is None:
            if not entered:
                return hold("spec entry condition not met", snapshot, latest_close)
            # A stopless spec is rejected by SpecParams; this guard is
            # defensive and keeps the BUY's stop provably positive.
            if signal_set.stop_loss is None:
                return hold("spec defines no stop_loss", snapshot, latest_close)
            stop = _compute_stop(signal_set.stop_loss, latest_close, candles)
            take_profit = _compute_take_profit(
                signal_set.take_profit, latest_close, stop, candles,
            )
            return SignalEvaluation(
                kind=SignalKind.BUY,
                reason="spec entry condition met",
                indicators=snapshot,
                proposed_entry_price=latest_close,
                proposed_stop_price=stop,
                proposed_take_profit_price=take_profit,
            )

        # Position open. Condition-exits and the time exit produce an EXIT
        # signal; stop-loss / take-profit are monitored by the execution
        # layer (the v1-template contract — the template emits only
        # signal-driven exits).
        time_exit = _time_exit_hit(signal_set.max_bars_held, position, candles)
        if exited or time_exit:
            reason = (
                "spec time exit (max bars held)"
                if time_exit and not exited
                else "spec exit condition met"
            )
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason=reason,
                indicators=snapshot,
                proposed_entry_price=latest_close,
                proposed_stop_price=position.stop_price,
            )
        return hold("position open, no spec exit signal", snapshot, latest_close)

    def _decide_from_kind(
        self,
        kind: SignalKind,
        candles: pd.DataFrame,
        position: PaperPosition | None,
    ) -> SignalEvaluation:
        """Build a SignalEvaluation for a Tier-3 decision handed back by
        `run_live_cycle`. Stop / take-profit prices come straight from the
        spec's exit methods (there is no SignalSet on the Tier-3 path).
        """
        latest_close = to_decimal(float(candles["close"].iloc[-1]))
        snapshot: IndicatorSnapshot = {
            "tier3_entry": 1.0 if kind is SignalKind.BUY else 0.0,
            "tier3_exit": 1.0 if kind is SignalKind.EXIT else 0.0,
        }
        if kind is SignalKind.BUY:
            stop_method = next(
                (e.method for e in self._spec.exit.exits if isinstance(e, StopLossExit)),
                None,
            )
            if stop_method is None:
                return hold("spec defines no stop_loss", snapshot, latest_close)
            stop = _compute_stop(stop_method, latest_close, candles)
            tp_method = next(
                (e.method for e in self._spec.exit.exits if isinstance(e, TakeProfitExit)),
                None,
            )
            return SignalEvaluation(
                kind=SignalKind.BUY,
                reason="tier-3 entry signal (prior_signal / prior_trade gate)",
                indicators=snapshot,
                proposed_entry_price=latest_close,
                proposed_stop_price=stop,
                proposed_take_profit_price=_compute_take_profit(
                    tp_method, latest_close, stop, candles,
                ),
            )
        if kind is SignalKind.EXIT and position is not None:
            return SignalEvaluation(
                kind=SignalKind.EXIT,
                reason="tier-3 exit signal",
                indicators=snapshot,
                proposed_entry_price=latest_close,
                proposed_stop_price=position.stop_price,
            )
        # HOLD, or an EXIT the live trader cannot act on (no real position).
        return hold("tier-3: no actionable signal", snapshot, latest_close)


def _compute_stop(method: StopLossMethod, entry: Decimal, candles: pd.DataFrame) -> Decimal:
    """Absolute long-side stop price from the spec's stop method.

    Trailing variants get their INITIAL stop here — live trailing is not
    part of A.5a. Mirrors `backtest.iterative._stop_level` but produces an
    absolute price (the trader's convention) rather than a vbt percent.
    """
    if isinstance(method, StopLossPercent | StopLossTrailingPercent):
        return quantize_price(entry * (Decimal(1) - Decimal(str(abs(method.value)))))
    if isinstance(method, StopLossAtrMultiple | StopLossTrailingAtr):
        atr_now = float(ind.atr(candles, method.atr_period).iloc[-1])
        return atr_stop_for_long(entry, to_decimal(atr_now), Decimal(str(method.mult)))
    # The remaining StopLossMethod variant is StopLossFixedPrice.
    return quantize_price(Decimal(str(method.price)))


def _compute_take_profit(
    method: TakeProfitMethod | None,
    entry: Decimal,
    stop: Decimal,
    candles: pd.DataFrame | None = None,
) -> Decimal | None:
    """Absolute long-side take-profit price, or None when the spec has no
    take-profit exit. r_multiple is measured off the stop distance;
    atr_multiple is measured off ATR(atr_period) at the current bar
    (so `candles` is required for the atr_multiple branch — defaults to
    None for backward-compat with non-ATR callers; raises if a
    TakeProfitAtrMultiple is supplied without candles).
    """
    if method is None:
        return None
    if isinstance(method, TakeProfitPercent):
        return quantize_price(entry * (Decimal(1) + Decimal(str(method.value))))
    if isinstance(method, TakeProfitFixedPrice):
        return quantize_price(Decimal(str(method.price)))
    if isinstance(method, TakeProfitAtrMultiple):
        # v1.2.E: symmetric to StopLossAtrMultiple's atr_multiple branch.
        # TP = entry + mult × ATR_at_current_bar.
        if candles is None:
            raise ValueError(
                "TakeProfitAtrMultiple requires the `candles` argument; "
                "the caller must pass the candle DataFrame so ATR can be "
                "computed at the entry bar",
            )
        atr_now = float(ind.atr(candles, method.atr_period).iloc[-1])
        return quantize_price(entry + Decimal(str(method.mult)) * to_decimal(atr_now))
    # The remaining TakeProfitMethod variant is TakeProfitRMultiple.
    return quantize_price(entry + Decimal(str(method.r)) * (entry - stop))


def _time_exit_hit(
    max_bars_held: int | None,
    position: PaperPosition,
    candles: pd.DataFrame,
) -> bool:
    """True if a `time` exit has elapsed — `max_bars_held` closed candles
    have printed since the position's entry. None when the spec has no
    time exit.
    """
    if max_bars_held is None:
        return False
    bars_held = sum(1 for ts in candles.index if ts > position.entry_ts)
    return bars_held >= max_bars_held


__all__ = ["SpecParams", "SpecTemplate", "spec_template_rejection_reason"]
