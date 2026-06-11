"""Tier-3 iterative backtest simulator (A.3b).

A Tier-3 spec — one using a `prior_trade` / `prior_signal` condition or
a `ratchet reset="per_trade"` expression — cannot be evaluated by the
vectorbt path: its truth at bar N depends on the *outcome* of trades or
signals that closed before N, which is sequential by nature. This module
walks the bars one at a time, maintaining a `TradeHistory` and a
`SignalHistory`, and is selected by the router in `engine.run_backtest`
only for such specs. Every other spec stays on the (faster, vectorised)
vectorbt path untouched.

`prior_signal` additionally needs a *phantom outcome* per skipped entry
signal: when a gate skips a signal, the simulator forward-simulates the
trade the entry would have produced (same exits, fees, fills) and scores
it win/loss/breakeven. Phantom outcomes feed `SignalHistory` only — they
never touch equity, the trade ledger, or any real metric. See
docs/design/v2-phase-a-stateful-conditions.md section 4.7.

This is a *bespoke* engine — it does not reproduce vectorbt's internal
float arithmetic bit-for-bit (design doc §4.6: the T3 path has its own
numerics). It is validated by `tests/test_backtest_control.py`, which
asserts it matches vectorbt *structurally* (identical trade count and
entry/exit timestamps) and *within tolerance* on the headline metrics.

Conventions, matched to the vectorbt path on purpose:

  * Fill timing. A signal is decided on bar N's CLOSE and filled at bar
    N+1's OPEN — both entries and signal/time exits. A trade's
    entry/exit *timestamp* is the SIGNAL bar (matching vectorbt, which
    records the order at the signal bar with a next-open fill price).
    The last bar has no successor, so a signal there cannot fill.
  * Stop-loss / take-profit fill INTRABAR the bar the price range
    crosses the level — recorded at that bar. A gap straight through
    fills at that bar's open (you cannot fill better than the gap).
  * Fees. `commission_pct` is charged on the entry notional and again
    on the exit notional. Slippage worsens the fill price: a buy fills
    `*(1 + slippage_pct)`, a sell `*(1 - slippage_pct)`.
  * An open position at the final bar is closed at the last close
    (`exit_reason="end_of_data"`), matching how vectorbt values it.

A.3b scope: long-only; position sizing is fixed_quantity or
fixed_percent_equity; entry filters are unsupported. Anything outside
that raises `IterativeBacktestError` rather than silently mis-modelling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import pandas as pd
import structlog
from marketmind_shared.schemas import BacktestMeta, BacktestRun, EquityPoint, Trade
from marketmind_shared.schemas.strategy_spec import (
    DEFAULT_COST_MODEL,
    DEFAULT_POSITION_SIZING,
    AndCondition,
    CompareCondition,
    Condition,
    ConditionExit,
    Direction,
    ExitRules,
    Expression,
    FixedPercentEquitySizing,
    FixedQuantitySizing,
    NotCondition,
    OrCondition,
    PositionSizing,
    PriorSignalCondition,
    PriorTradeCondition,
    RatchetExpr,
    RiskBasedSizing,
    RMultipleExit,
    ScaledExpr,
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
    TimeExit,
    Timeframe,
    decompose_r_multiple,
)
from marketmind_shared.schemas.strategy_spec.introspection import (
    condition_uses_prior_signal,
    condition_uses_tier3,
    iter_expressions,
)

from marketmind_workers.backtest import indicators as ind
from marketmind_workers.backtest.fee_model import commission_for_spec, default_fee_model
from marketmind_workers.backtest.slippage_model import (
    default_slippage_model,
    slippage_for_spec,
)
from marketmind_workers.backtest.trade_history import (
    CompletedTrade,
    SignalHistory,
    TradeHistory,
    classify_outcome,
)

# The Tier-3 simulator reuses the proven condition/expression evaluator
# from its sibling `translator` module rather than duplicating it. These
# names are underscore-prefixed there; the cross-module use is deliberate
# package-internal API, hence the localized pyright suppressions.
from marketmind_workers.backtest.translator import (
    _classify_entry_diagnostics,  # pyright: ignore[reportPrivateUsage]
    _Context,  # pyright: ignore[reportPrivateUsage]
    _estimate_warmup_bars,  # pyright: ignore[reportPrivateUsage]
    _eval_condition,  # pyright: ignore[reportPrivateUsage]
    _eval_expression,  # pyright: ignore[reportPrivateUsage]
)

log = structlog.get_logger(__name__)

_EntryEval = Callable[[int, TradeHistory, SignalHistory], bool]
_ExitEval = Callable[[int, int], bool]


class IterativeBacktestError(Exception):
    """Raised for Tier-3 spec shapes the iterative simulator cannot run."""


# ---- per-bar condition evaluators -----------------------------------------
#
# A Tier-3 condition tree is split: every maximal sub-tree that is NOT
# Tier-3 is pre-computed once as a vectorised bool Series (reusing the
# proven translator), and only the genuinely outcome-dependent leaves —
# `prior_trade`, `prior_signal`, and `ratchet reset="per_trade"` — are
# evaluated per bar.


def _expr_uses_per_trade_ratchet(expr: Expression) -> bool:
    """True if `expr` contains a ratchet with reset='per_trade'."""
    return any(
        isinstance(e, RatchetExpr) and e.reset == "per_trade" for e in iter_expressions(expr)
    )


def _ratchet_and_scale(expr: Expression) -> tuple[RatchetExpr, float]:
    """Unwrap `scaled(factor, ... ratchet(...))` into the RatchetExpr and a
    cumulative scale factor. A bare ratchet has scale 1.0.
    """
    scale = 1.0
    while isinstance(expr, ScaledExpr):
        scale *= expr.factor
        expr = expr.expression
    if not isinstance(expr, RatchetExpr):
        raise IterativeBacktestError(
            "a per-trade ratchet must be a bare ratchet or scaled(ratchet); "
            f"got {type(expr).__name__}",
        )
    return expr, scale


def _build_entry_evaluator(cond: Condition, ctx: _Context) -> _EntryEval:
    """Compile an entry condition into `eval(bar, trades, signals) -> bool`.

    Non-Tier-3 sub-trees are pre-computed to a bool Series; `prior_trade`
    leaves consult the live `TradeHistory`, `prior_signal` leaves the
    live `SignalHistory`. A per-trade ratchet inside an entry condition
    is ill-defined (there is no open trade to anchor it) and is rejected.
    """
    if not condition_uses_tier3(cond):
        series = _eval_condition(cond, ctx).fillna(value=False).astype(bool).to_numpy()
        return lambda bar, _trades, _signals: bool(series[bar])
    if isinstance(cond, AndCondition):
        children = [_build_entry_evaluator(c, ctx) for c in cond.conditions]
        return lambda bar, trades, signals: all(
            child(bar, trades, signals) for child in children
        )
    if isinstance(cond, OrCondition):
        children = [_build_entry_evaluator(c, ctx) for c in cond.conditions]
        return lambda bar, trades, signals: any(
            child(bar, trades, signals) for child in children
        )
    if isinstance(cond, NotCondition):
        inner = _build_entry_evaluator(cond.condition, ctx)
        return lambda bar, trades, signals: not inner(bar, trades, signals)
    if isinstance(cond, PriorTradeCondition):
        predicate, n = cond.predicate, cond.n
        # v1.2.B: pass the current bar through so the new
        # bars_since_last_at_least predicate can compute elapsed time.
        # The four outcome-based predicates ignore current_bar (it's a
        # keyword-only default-None parameter on evaluate_predicate).
        return lambda bar, trades, _signals: trades.evaluate_predicate(
            predicate, n, current_bar=bar,
        )
    if isinstance(cond, PriorSignalCondition):
        sig_predicate = cond.predicate
        return lambda bar, _trades, signals: signals.evaluate_predicate(sig_predicate, bar)
    raise IterativeBacktestError(
        f"Tier-3 entry condition shape not supported by the A.3b simulator: "
        f"{type(cond).__name__} (per-trade ratchets belong in exit conditions)",
    )


def _build_raw_signal(entry: Condition, ctx: _Context) -> list[bool]:
    """The raw-signal series for a `prior_signal` entry: True on the bars
    the entry's non-gate *core* condition fires.

    A `prior_signal` entry must take the shape `and(core..., gate...)` —
    the `core` children (none Tier-3) generate signals, the `gate`
    children (each Tier-3) decide fire vs skip. The raw signal is the AND
    of the core children. It is what lets the simulator tell a *skipped*
    signal (raw True, gate blocked) from a *non-signal* (raw False) — a
    distinction `entry_eval` alone, which folds the gate in, cannot make.

    Entry shapes outside `and(core, gate)` raise rather than be silently
    mis-modelled — the simulator's standing fail-loud policy.
    """
    if not isinstance(entry, AndCondition):
        raise IterativeBacktestError(
            "a prior_signal entry must be `and(<signal>, <gate>)`; the "
            f"iterative engine cannot identify 'a signal' inside a bare "
            f"{type(entry).__name__}",
        )
    core = [c for c in entry.conditions if not condition_uses_tier3(c)]
    if not core:
        raise IterativeBacktestError(
            "a prior_signal entry has a gate but no non-stateful core "
            "signal — there is nothing for prior_signal to refer to",
        )
    series: pd.Series | None = None
    for c in core:
        part = _eval_condition(c, ctx).fillna(value=False).astype(bool)
        series = part if series is None else cast("pd.Series", series & part)
    assert series is not None
    return [bool(v) for v in series.to_numpy()]


def _build_exit_evaluator(cond: Condition, ctx: _Context) -> _ExitEval:
    """Compile a condition-exit into `eval(bar, entry_bar) -> bool`.

    `entry_bar` is consulted only by per-trade ratchets, whose running
    extremum is taken over the open trade's window [entry_bar, bar].
    """
    if not condition_uses_tier3(cond):
        series = _eval_condition(cond, ctx).fillna(value=False).astype(bool).to_numpy()
        return lambda bar, _entry_bar: bool(series[bar])
    if isinstance(cond, AndCondition):
        children = [_build_exit_evaluator(c, ctx) for c in cond.conditions]
        return lambda bar, entry_bar: all(child(bar, entry_bar) for child in children)
    if isinstance(cond, OrCondition):
        children = [_build_exit_evaluator(c, ctx) for c in cond.conditions]
        return lambda bar, entry_bar: any(child(bar, entry_bar) for child in children)
    if isinstance(cond, NotCondition):
        inner = _build_exit_evaluator(cond.condition, ctx)
        return lambda bar, entry_bar: not inner(bar, entry_bar)
    if isinstance(cond, CompareCondition):
        return _build_ratchet_compare_evaluator(cond, ctx)
    raise IterativeBacktestError(
        f"Tier-3 exit condition shape not supported by the A.3b simulator: "
        f"{type(cond).__name__}",
    )


def _build_ratchet_compare_evaluator(cond: CompareCondition, ctx: _Context) -> _ExitEval:
    """Compile a `compare` whose ratchet side is a per-trade ratchet.

    The non-ratchet side is pre-computed to a Series; the ratchet side is
    the running extremum of its source over [entry_bar, bar], scaled.
    """
    tf = ctx.spec.primary_timeframe
    left_t3 = _expr_uses_per_trade_ratchet(cond.left)
    right_t3 = _expr_uses_per_trade_ratchet(cond.right)
    if left_t3 == right_t3:
        raise IterativeBacktestError(
            "a Tier-3 exit compare must have a per-trade ratchet on exactly one side",
        )
    ratchet_side = cond.left if left_t3 else cond.right
    static_side = cond.right if left_t3 else cond.left
    ratchet, scale = _ratchet_and_scale(ratchet_side)
    static = [float(v) for v in _eval_expression(static_side, ctx, timeframe=tf).to_numpy()]
    source = [float(v) for v in _eval_expression(ratchet.source, ctx, timeframe=tf).to_numpy()]
    is_max = ratchet.extremum == "max"
    op = cond.op
    ratchet_on_left = left_t3

    def _evaluate(bar: int, entry_bar: int) -> bool:
        window = source[entry_bar : bar + 1]
        if not window:
            return False
        extremum = max(window) if is_max else min(window)
        ratchet_val = extremum * scale
        static_val = static[bar]
        if ratchet_val != ratchet_val or static_val != static_val:  # NaN guard
            return False
        left = ratchet_val if ratchet_on_left else static_val
        right = static_val if ratchet_on_left else ratchet_val
        return _apply_op(op, left, right)

    return _evaluate


def _apply_op(op: str, left: float, right: float) -> bool:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    return left == right


# ---- position state --------------------------------------------------------


@dataclass
class _Position:
    """One open long position inside the simulator."""

    entry_bar: int        # the entry SIGNAL bar (timestamp anchor)
    entry_fill: float     # fill price, slippage included
    size: float           # base-currency units held
    stop_level: float | None
    tp_level: float | None
    trail_anchor: float   # running favourable extreme, for trailing stops


@dataclass(frozen=True)
class _PhantomMachinery:
    """The extra state a `prior_signal` spec needs (None for every other
    spec): the raw-signal series and the phantom-outcome evaluator.
    """

    raw_signal: list[bool]
    # phantom(signal_bar) -> (net return %, resolved bar) of the trade the
    # entry would have produced had it fired at signal_bar.
    phantom: Callable[[int], tuple[float, int]]


# ---- public entry point ----------------------------------------------------


def run_iterative_backtest(
    spec: StrategySpec,
    data: dict[Timeframe, pd.DataFrame],
    start: datetime,
    end: datetime,
    initial_capital: float = 10_000.0,
) -> BacktestRun:
    """Sequentially backtest a Tier-3 `spec`. Long-only.

    `data` maps Timeframe -> OHLCV DataFrame (same as `build_signals`).
    Returns a `BacktestRun` in the identical shape the vectorbt path
    produces, so `compute_metrics` and everything downstream is unchanged.
    """
    if spec.direction is not Direction.LONG:
        raise IterativeBacktestError(
            "the A.3b Tier-3 iterative simulator is long-only; "
            f"got direction={spec.direction.value}",
        )
    if spec.filters:
        raise IterativeBacktestError(
            "entry filters are not supported by the A.3b iterative path",
        )
    primary_df = data[spec.primary_timeframe]
    index = primary_df.index
    if not isinstance(index, pd.DatetimeIndex):
        raise IterativeBacktestError("primary OHLCV index must be a DatetimeIndex")
    ctx = _Context(spec=spec, data=data, primary_index=index)

    opens = [float(v) for v in primary_df["open"].to_numpy()]
    highs = [float(v) for v in primary_df["high"].to_numpy()]
    lows = [float(v) for v in primary_df["low"].to_numpy()]
    closes = [float(v) for v in primary_df["close"].to_numpy()]
    n = len(closes)

    entry_eval = _build_entry_evaluator(spec.entry.condition, ctx)
    cond_exits, stop_method, tp_method, max_bars = _compile_exit_rules(spec.exit, ctx)
    commission, slippage = _resolve_costs(spec)
    sizing: PositionSizing = spec.position_sizing
    atr = _atr_for_stop(stop_method, primary_df, tp_method)

    history = TradeHistory()
    signal_history = SignalHistory()
    # A prior_signal entry needs the raw-signal series + phantom evaluator.
    # Every other spec leaves this None and skips the machinery entirely.
    phantom_machinery: _PhantomMachinery | None = None
    if condition_uses_prior_signal(spec.entry.condition):
        phantom_machinery = _PhantomMachinery(
            raw_signal=_build_raw_signal(spec.entry.condition, ctx),
            phantom=_build_phantom_evaluator(
                opens, highs, lows, closes, n, commission, slippage,
                stop_method, tp_method, max_bars, cond_exits, atr,
            ),
        )
    cash = initial_capital
    position: _Position | None = None
    trades: list[Trade] = []
    equity: list[EquityPoint] = []
    entry_flags: list[bool] = []
    pending_entry_bar: int | None = None
    pending_exit_bar: int | None = None
    trade_id = 0

    for bar in range(n):
        # STEP 1 — execute next-open fills decided on the previous bar.
        if pending_entry_bar is not None and position is None:
            fill = opens[bar]
            if fill == fill:  # not NaN
                position = _open_position(
                    pending_entry_bar, fill, cash, commission, slippage,
                    sizing, stop_method, tp_method, atr,
                )
                if position is not None:
                    cash -= position.size * position.entry_fill * (1.0 + commission)
            pending_entry_bar = None
        if pending_exit_bar is not None and position is not None:
            fill = opens[bar]
            if fill == fill:
                trade_id += 1
                cash, trade = _close_position(
                    position, pending_exit_bar, fill, commission, slippage,
                    index, "signal", trade_id, cash,
                )
                trades.append(trade)
                history.record(_completed(trade, position, trade_id))
                if phantom_machinery is not None:
                    signal_history.resolve_last_pending(trade.return_pct, resolved_bar=bar)
                position = None
            pending_exit_bar = None

        # STEP 2 — intrabar stop-loss / take-profit for an open position.
        if position is not None:
            hit_price, reason = _intrabar_exit(position, lows[bar], highs[bar], opens[bar])
            if hit_price is not None:
                trade_id += 1
                cash, trade = _close_position(
                    position, bar, hit_price, commission, slippage,
                    index, reason, trade_id, cash,
                )
                trades.append(trade)
                history.record(_completed(trade, position, trade_id))
                if phantom_machinery is not None:
                    signal_history.resolve_last_pending(trade.return_pct, resolved_bar=bar)
                position = None
            else:
                _update_trailing(position, stop_method, highs[bar], atr, bar)

        # STEP 3 — mark equity to market on this bar's close.
        units = position.size if position is not None else 0.0
        equity.append(EquityPoint(timestamp=_utc(index[bar]), value=cash + units * closes[bar]))

        # STEP 4 — evaluate the entry condition every bar (for diagnostics);
        #          act on it only when flat. Signals fill at the NEXT open.
        fired = entry_eval(bar, history, signal_history)
        entry_flags.append(fired)
        if position is None and pending_entry_bar is None and bar < n - 1:
            if phantom_machinery is None:
                # No prior_signal: fire whenever the (gated) entry is true.
                if fired:
                    pending_entry_bar = bar
            elif phantom_machinery.raw_signal[bar]:
                # A raw signal fired while flat. `fired` folds in the
                # prior_signal gate: true => the gate opened (a real
                # entry), false => the gate skipped it, so phantom-score
                # the trade the entry would have produced.
                if fired:
                    pending_entry_bar = bar
                    signal_history.record_fired(signal_bar=bar)
                else:
                    phantom_return, phantom_exit = phantom_machinery.phantom(bar)
                    signal_history.record_skipped(
                        signal_bar=bar,
                        return_pct=phantom_return,
                        resolved_bar=phantom_exit,
                    )

        # STEP 5 — signal / time exits, decided at close, filled next open.
        if position is not None and pending_exit_bar is None:
            bars_held = bar - position.entry_bar
            time_hit = max_bars is not None and bars_held >= max_bars
            cond_hit = any(ev(bar, position.entry_bar) for ev in cond_exits)
            if (time_hit or cond_hit) and bar < n - 1:
                pending_exit_bar = bar

    # An open position at the final bar is valued at the last close.
    if position is not None:
        trade_id += 1
        cash, trade = _close_position(
            position, n - 1, closes[n - 1], commission, slippage,
            index, "end_of_data", trade_id, cash,
        )
        trades.append(trade)

    diagnostics = _classify_entry_diagnostics(
        pd.Series(entry_flags, index=index, dtype=object),
        warmup_bars=_estimate_warmup_bars(spec),
    )
    meta = BacktestMeta(
        symbol=spec.instrument.symbol,
        primary_timeframe=spec.primary_timeframe,
        filter_timeframe=spec.filter_timeframe,
        start=start,
        end=end,
        initial_capital=initial_capital,
        direction=spec.direction,
        defaulted_costs=spec.costs == DEFAULT_COST_MODEL,
        defaulted_position_sizing=spec.position_sizing == DEFAULT_POSITION_SIZING,
    )
    log.info(
        "iterative_backtest_complete",
        spec=spec.name,
        symbol=spec.instrument.symbol,
        bars=n,
        trades=len(trades),
    )
    return BacktestRun(
        spec_name=spec.name,
        meta=meta,
        equity_curve=equity,
        trades=trades,
        entry_diagnostics=diagnostics,
    )


# ---- exit-rule compilation -------------------------------------------------


def _compile_exit_rules(
    exit_rules: ExitRules,
    ctx: _Context,
) -> tuple[list[_ExitEval], StopLossMethod | None, TakeProfitMethod | None, int | None]:
    """Split the spec's exits into per-bar condition evaluators, a single
    stop-loss method, a single take-profit method, and a time limit.
    """
    cond_exits: list[_ExitEval] = []
    stop_method: StopLossMethod | None = None
    tp_method: TakeProfitMethod | None = None
    max_bars: int | None = None
    for ex in exit_rules.exits:
        if isinstance(ex, ConditionExit):
            cond_exits.append(_build_exit_evaluator(ex.condition, ctx))
        elif isinstance(ex, StopLossExit):
            stop_method = ex.method
        elif isinstance(ex, TakeProfitExit):
            tp_method = ex.method
        elif isinstance(ex, TimeExit):
            max_bars = ex.max_bars_held
        else:
            # The remaining ExitCondition variant is RMultipleExit
            # (primitive-4) — a PRIMARY exit. Synthesize the ATR-multiple
            # stop + take-profit it composes (R = atr_multiple × ATR), so
            # the same _atr_for_stop / _stop_level / _tp_level at-entry math
            # and the same stop-before-target intrabar priority drive it.
            assert isinstance(ex, RMultipleExit)
            stop_method, tp_method = decompose_r_multiple(ex)
    return cond_exits, stop_method, tp_method, max_bars


def _resolve_costs(spec: StrategySpec) -> tuple[float, float]:
    # Commission via the FeeModel (Phase B.1); slippage via the
    # SlippageModel (Phase B.2). Neither reads spec.costs anymore.
    commission = commission_for_spec(spec, side="taker", model=default_fee_model())
    slippage = slippage_for_spec(spec, side="taker", model=default_slippage_model())
    return commission, slippage


def _atr_for_stop(
    stop_method: StopLossMethod | None,
    df: pd.DataFrame,
    tp_method: TakeProfitMethod | None = None,
) -> list[float] | None:
    """Pre-compute the ATR series the stop OR take-profit needs.

    Returns None when neither exit method is ATR-based. If both are
    ATR-based and use different atr_period values, the stop's period
    wins (the legacy semantic predating v1.2.E). Same-period mix is
    the dominant real-world pattern — e.g. 14-period ATR for both a
    trailing stop and an R:R take-profit.

    v1.2.E (2026-05-25): tp_method parameter added with default None
    to keep existing call sites byte-identical (the v1.2.B-style
    keyword-default-None signature widening pattern).
    """
    if isinstance(stop_method, StopLossAtrMultiple | StopLossTrailingAtr):
        return [float(v) for v in ind.atr(df, stop_method.atr_period).to_numpy()]
    if isinstance(tp_method, TakeProfitAtrMultiple):
        return [float(v) for v in ind.atr(df, tp_method.atr_period).to_numpy()]
    return None


# ---- position open / close -------------------------------------------------


def _open_position(
    entry_bar: int,
    raw_price: float,
    cash: float,
    commission: float,
    slippage: float,
    sizing: PositionSizing,
    stop_method: StopLossMethod | None,
    tp_method: TakeProfitMethod | None,
    atr: list[float] | None,
) -> _Position | None:
    """Open a long position. Returns None if it cannot be sized."""
    entry_fill = raw_price * (1.0 + slippage)
    if entry_fill <= 0.0:
        return None
    stop_level = _stop_level(stop_method, entry_fill, atr, entry_bar)
    tp_level = _tp_level(tp_method, entry_fill, stop_level, atr, entry_bar)
    size = _entry_size(
        sizing,
        cash,
        entry_fill,
        commission,
        stop_method=stop_method,
        atr=atr,
        entry_bar=entry_bar,
    )
    if size <= 0.0:
        return None
    return _Position(
        entry_bar=entry_bar,
        entry_fill=entry_fill,
        size=size,
        stop_level=stop_level,
        tp_level=tp_level,
        trail_anchor=entry_fill,
    )


def _entry_size(
    sizing: PositionSizing,
    cash: float,
    entry_fill: float,
    commission: float,
    *,
    stop_method: StopLossMethod | None = None,
    atr: list[float] | None = None,
    entry_bar: int | None = None,
) -> float:
    """Base-currency units to buy. fixed_percent_equity deploys that
    fraction of cash *including* the entry fee.

    v1.2-followup (2026-05-25, post-Hunt-7): RiskBasedSizing is now
    supported when paired with StopLossPercent / StopLossAtrMultiple /
    StopLossTrailingAtr. The math mirrors the vbt engine's _vbt_size
    branch exactly so the cross-engine drift-parity gate stays clean.
    The new stop_method / atr / entry_bar parameters are keyword-only
    with None defaults (v1.2.B pattern) so existing callers stay
    byte-identical.
    """
    if isinstance(sizing, FixedQuantitySizing):
        return sizing.quantity
    if isinstance(sizing, FixedPercentEquitySizing):
        return (sizing.percent * cash) / (entry_fill * (1.0 + commission))
    # The remaining PositionSizing variant is RiskBasedSizing.
    assert isinstance(sizing, RiskBasedSizing)
    if stop_method is None:
        raise IterativeBacktestError(
            "risk_based position sizing requires a stop_loss exit; "
            "the Phase 1 validator should have rejected this spec",
        )
    if isinstance(stop_method, StopLossPercent):
        stop_pct = float(abs(stop_method.value))
        if stop_pct == 0.0:
            raise IterativeBacktestError(
                "risk_based sizing requires non-zero stop_loss percent",
            )
        size_pct = min(sizing.risk_percent / stop_pct, 1.0)
        return (size_pct * cash) / (entry_fill * (1.0 + commission))
    if isinstance(stop_method, StopLossAtrMultiple | StopLossTrailingAtr):
        # Identical to _vbt_size's branch: the INITIAL stop distance
        # at the entry bar drives sizing. Trail ratchet only affects
        # exit-side behaviour, not entry-bar position size.
        if atr is None or entry_bar is None:
            raise IterativeBacktestError(
                "risk_based + ATR-based stop requires atr series + entry_bar",
            )
        atr_at_entry = atr[entry_bar]
        if atr_at_entry != atr_at_entry:  # NaN during warmup
            return 0.0
        stop_distance = stop_method.mult * atr_at_entry
        if stop_distance <= 0.0:
            return 0.0
        stop_pct = stop_distance / entry_fill
        size_pct = min(sizing.risk_percent / stop_pct, 1.0)
        return (size_pct * cash) / (entry_fill * (1.0 + commission))
    # Unsupported stop variants surface the same Phase-3.1 message the
    # vbt path raises, for symmetric operator experience.
    raise IterativeBacktestError(
        f"risk_based sizing is not supported with stop method "
        f"{type(stop_method).__name__}",
    )


def _close_position(
    pos: _Position,
    exit_bar: int,
    raw_price: float,
    commission: float,
    slippage: float,
    index: pd.DatetimeIndex,
    reason: str,
    trade_id: int,
    cash: float,
) -> tuple[float, Trade]:
    """Close `pos`, returning the new cash balance and the Trade record."""
    exit_fill = raw_price * (1.0 - slippage)
    entry_cost = pos.size * pos.entry_fill * (1.0 + commission)
    exit_proceeds = pos.size * exit_fill * (1.0 - commission)
    pnl = exit_proceeds - entry_cost
    return_pct = pnl / entry_cost if entry_cost != 0.0 else 0.0
    trade = Trade(
        entry_time=_utc(index[pos.entry_bar]),
        exit_time=_utc(index[exit_bar]),
        entry_price=pos.entry_fill,
        exit_price=exit_fill,
        size=pos.size,
        pnl=pnl,
        return_pct=return_pct,
        direction=Direction.LONG,
        exit_reason=reason,
    )
    return cash + exit_proceeds, trade


def _completed(trade: Trade, pos: _Position, trade_id: int) -> CompletedTrade:
    return CompletedTrade(
        trade_id=trade_id,
        entry_index=pos.entry_bar,
        exit_index=trade_id,  # close order; only ordering matters to TradeHistory
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        pnl=trade.pnl,
        return_pct=trade.return_pct,
        outcome=classify_outcome(trade.return_pct),
    )


# ---- stop-loss / take-profit -----------------------------------------------


def _stop_level(
    stop_method: StopLossMethod | None,
    entry_fill: float,
    atr: list[float] | None,
    entry_bar: int,
) -> float | None:
    """Initial stop price for a long position (trailing stops trail up
    from here on later bars).
    """
    if stop_method is None:
        return None
    if isinstance(stop_method, StopLossPercent | StopLossTrailingPercent):
        return entry_fill * (1.0 - abs(stop_method.value))
    if isinstance(stop_method, StopLossAtrMultiple | StopLossTrailingAtr):
        atr_at_entry = (atr[entry_bar] if atr is not None else 0.0)
        if atr_at_entry != atr_at_entry:  # NaN during warmup
            atr_at_entry = 0.0
        return entry_fill - stop_method.mult * atr_at_entry
    # The remaining StopLossMethod variant is StopLossFixedPrice.
    return stop_method.price


def _tp_level(
    tp_method: TakeProfitMethod | None,
    entry_fill: float,
    stop_level: float | None,
    atr: list[float] | None,
    entry_bar: int,
) -> float | None:
    """Take-profit price for a long position.

    `atr` is the pre-computed ATR series the position needs for the
    atr_multiple variant — same series the trailing-ATR / atr-multiple
    stop uses (computed once by ``_atr_for_position``). None for
    non-ATR-using TP methods. The iterative engine is long-only, so the
    formula is always entry + magnitude.
    """
    if tp_method is None:
        return None
    if isinstance(tp_method, TakeProfitPercent):
        return entry_fill * (1.0 + tp_method.value)
    if isinstance(tp_method, TakeProfitFixedPrice):
        return tp_method.price
    if isinstance(tp_method, TakeProfitAtrMultiple):
        # v1.2.E: symmetric to StopLossAtrMultiple's at-entry math.
        # ATR at entry = atr[entry_bar]; NaN during warmup collapses
        # to 0 (the trade gets an effectively-immediate target, same
        # graceful-degradation pattern as _stop_level's NaN handler).
        if atr is None:
            raise IterativeBacktestError(
                "TakeProfitAtrMultiple requires the ATR series; ensure "
                "the engine pre-computes ATR for this position (see "
                "_atr_for_position)",
            )
        atr_at_entry = atr[entry_bar]
        if atr_at_entry != atr_at_entry:  # NaN during warmup
            atr_at_entry = 0.0
        return entry_fill + tp_method.mult * atr_at_entry
    # The remaining TakeProfitMethod variant is TakeProfitRMultiple.
    if stop_level is None:
        raise IterativeBacktestError("r_multiple take-profit requires a stop-loss")
    risk = entry_fill - stop_level
    return entry_fill + tp_method.r * risk


def _update_trailing(
    pos: _Position,
    stop_method: StopLossMethod | None,
    high: float,
    atr: list[float] | None,
    bar: int,
) -> None:
    """Ratchet a trailing stop up after this bar's high. Fixed stops are
    left untouched. Called after the intrabar check, so the new level
    only applies from the NEXT bar (no intrabar look-ahead).
    """
    if pos.stop_level is None:
        return
    if isinstance(stop_method, StopLossTrailingPercent):
        pos.trail_anchor = max(pos.trail_anchor, high)
        pos.stop_level = max(pos.stop_level, pos.trail_anchor * (1.0 - abs(stop_method.value)))
    elif isinstance(stop_method, StopLossTrailingAtr):
        pos.trail_anchor = max(pos.trail_anchor, high)
        atr_now = atr[bar] if atr is not None else 0.0
        if atr_now == atr_now:  # not NaN
            pos.stop_level = max(pos.stop_level, pos.trail_anchor - stop_method.mult * atr_now)


def _intrabar_exit(
    pos: _Position,
    low: float,
    high: float,
    open_: float,
) -> tuple[float | None, str]:
    """Check whether this bar's range triggers the stop or take-profit.

    Stop is checked before take-profit (the conservative resolution of an
    ambiguous same-bar hit). A gap straight through a level fills at the
    bar's open — you cannot fill better than the gap.
    """
    if pos.stop_level is not None and low <= pos.stop_level:
        fill = open_ if open_ <= pos.stop_level else pos.stop_level
        return fill, "stop_loss"
    if pos.tp_level is not None and high >= pos.tp_level:
        fill = open_ if open_ >= pos.tp_level else pos.tp_level
        return fill, "take_profit"
    return None, ""


# ---- phantom outcomes (prior_signal) ---------------------------------------


def _phantom_return(
    entry_fill: float,
    raw_exit_price: float,
    commission: float,
    slippage: float,
) -> float:
    """Net post-cost return % of a phantom trade — the same arithmetic
    `_close_position` applies, with the position size cancelled out (a
    return % does not depend on size, so a phantom needs no sizing).
    """
    exit_fill = raw_exit_price * (1.0 - slippage)
    entry_cost = entry_fill * (1.0 + commission)
    exit_proceeds = exit_fill * (1.0 - commission)
    if entry_cost == 0.0:
        return 0.0
    return (exit_proceeds - entry_cost) / entry_cost


def _build_phantom_evaluator(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    n: int,
    commission: float,
    slippage: float,
    stop_method: StopLossMethod | None,
    tp_method: TakeProfitMethod | None,
    max_bars: int | None,
    cond_exits: list[_ExitEval],
    atr: list[float] | None,
) -> Callable[[int], tuple[float, int]]:
    """Compile a phantom-outcome evaluator for a `prior_signal` spec.

    `phantom(signal_bar)` simulates the trade the entry would have
    produced had it fired at `signal_bar`: a next-open fill, the spec's
    real stop / take-profit / time / condition exits, the real fee and
    slippage model. It returns `(return_pct, resolved_bar)` — the net
    post-cost return and the bar position the would-have-been trade
    closes on.

    The simulation mirrors `run_iterative_backtest`'s open-position
    lifecycle bar-for-bar (pending next-open fill, then intrabar
    stop/TP, then a close-decided/next-open-filled signal or time exit),
    so a phantom outcome equals what a real trade entered on the same
    signal would have produced — the property `test_iterative.py`
    pins directly. It is pure arithmetic over the frozen price arrays:
    deterministic, and it never touches cash, equity, or the trade
    ledger.
    """

    def _phantom(signal_bar: int) -> tuple[float, int]:
        # Callers only phantom-score signals with signal_bar < n - 1, so
        # the next-open fill bar always exists.
        fill_bar = signal_bar + 1
        raw_fill = opens[fill_bar]
        if raw_fill != raw_fill or raw_fill <= 0.0:  # NaN / non-positive open
            return 0.0, fill_bar
        entry_fill = raw_fill * (1.0 + slippage)
        stop_level = _stop_level(stop_method, entry_fill, atr, signal_bar)
        tp_level = _tp_level(tp_method, entry_fill, stop_level, atr, signal_bar)
        pos = _Position(
            entry_bar=signal_bar,
            entry_fill=entry_fill,
            size=1.0,
            stop_level=stop_level,
            tp_level=tp_level,
            trail_anchor=entry_fill,
        )
        pending_exit_bar: int | None = None
        for bar in range(fill_bar, n):
            # A signal/time exit decided last bar fills at this open.
            if pending_exit_bar is not None:
                return _phantom_return(entry_fill, opens[bar], commission, slippage), bar
            # Intrabar stop / take-profit — same stop-before-TP precedence
            # as a real trade. Trailing updates only when nothing hit.
            hit_price, _reason = _intrabar_exit(pos, lows[bar], highs[bar], opens[bar])
            if hit_price is not None:
                return _phantom_return(entry_fill, hit_price, commission, slippage), bar
            _update_trailing(pos, stop_method, highs[bar], atr, bar)
            # Signal / time exit, decided at this close, filled next open.
            bars_held = bar - signal_bar
            time_hit = max_bars is not None and bars_held >= max_bars
            cond_hit = any(ev(bar, signal_bar) for ev in cond_exits)
            if (time_hit or cond_hit) and bar < n - 1:
                pending_exit_bar = bar
        # Still open at the final bar — valued at the last close, exactly
        # as run_iterative_backtest values a real open position.
        return _phantom_return(entry_fill, closes[n - 1], commission, slippage), n - 1

    return _phantom


# ---- misc ------------------------------------------------------------------


def _utc(ts: object) -> datetime:
    """Coerce a pandas index value into a UTC datetime."""
    stamp = pd.Timestamp(cast("pd.Timestamp", ts))
    if stamp.tz is None:
        stamp = stamp.tz_localize(UTC)
    return cast("datetime", stamp.to_pydatetime())


__all__ = ["IterativeBacktestError", "run_iterative_backtest"]
