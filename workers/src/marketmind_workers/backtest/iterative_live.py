"""A.6 — live Tier-3 incremental stepper (the B3 sibling evaluator).

`iterative.py`'s `run_iterative_backtest` is a monolithic full-history
forward walk; the live trader must evaluate a `prior_signal` /
`prior_trade` spec one cycle at a time, resuming from a persisted
checkpoint. This module is that incremental stepper.

Per design doc §6C decision Q1 (B3): a *sibling* evaluator. It reuses
every per-bar primitive from `iterative.py` — `_open_position`,
`_close_position`, `_intrabar_exit`, `_update_trailing`, `_completed`,
`_stop_level`, `_tp_level`, `_phantom_return`, the `_build_*_evaluator`
compilers, `_compile_exit_rules` — and leaves `iterative.py` **untouched**.
The only un-shared code is the loop skeleton, gated bit-for-bit against
`run_iterative_backtest` by the A.6 drift-parity test.

Two deliberate differences from the backtest loop (design doc §6C):

  * **No `bar < n-1` guard.** In the backtest the last bar is end-of-data
    — a signal/exit there cannot fill, so it is skipped. In the live
    stepper the latest bar is just the latest *so far*: a signal/exit
    there is recorded `pending` and fills on the next cycle. The
    drift-parity test therefore compares the *settled* region only.
  * **Incremental phantoms.** A skipped signal becomes a pending phantom
    — a mini-position advanced one bar per cycle (design doc §6C.3) —
    rather than `_phantom`'s forward peek (the live trader has no forward
    bars). Each step of the phantom is `_phantom`'s loop body for one bar.

The shadow simulation evolves `cash` exactly as `run_iterative_backtest`
does — position sizes, and therefore the drift-parity comparison, then
match it bit-for-bit. Tier-3 *gating* (win/loss outcomes) is itself
size-independent; cash is tracked purely for that parity fidelity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from marketmind_shared.schemas.strategy_spec import (
    Direction,
    PositionSizing,
    StopLossMethod,
    StrategySpec,
    TakeProfitMethod,
    Timeframe,
)
from marketmind_shared.schemas.strategy_spec.introspection import condition_uses_prior_signal
from marketmind_shared.schemas.trader import (
    SignalKind,
    Tier3CompletedTrade,
    Tier3PendingPhantom,
    Tier3ShadowPosition,
    Tier3SignalRecord,
    Tier3State,
)

from marketmind_workers.backtest.iterative import (
    IterativeBacktestError,
    _atr_for_stop,  # pyright: ignore[reportPrivateUsage]
    _build_entry_evaluator,  # pyright: ignore[reportPrivateUsage]
    _build_raw_signal,  # pyright: ignore[reportPrivateUsage]
    _close_position,  # pyright: ignore[reportPrivateUsage]
    _compile_exit_rules,  # pyright: ignore[reportPrivateUsage]
    _completed,  # pyright: ignore[reportPrivateUsage]
    _ExitEval,  # pyright: ignore[reportPrivateUsage]
    _intrabar_exit,  # pyright: ignore[reportPrivateUsage]
    _open_position,  # pyright: ignore[reportPrivateUsage]
    _phantom_return,  # pyright: ignore[reportPrivateUsage]
    _Position,  # pyright: ignore[reportPrivateUsage]
    _resolve_costs,  # pyright: ignore[reportPrivateUsage]
    _stop_level,  # pyright: ignore[reportPrivateUsage]
    _tp_level,  # pyright: ignore[reportPrivateUsage]
    _update_trailing,  # pyright: ignore[reportPrivateUsage]
)
from marketmind_workers.backtest.trade_history import (
    CompletedTrade,
    SignalHistory,
    SignalRecord,
    TradeHistory,
    TradeOutcome,
)
from marketmind_workers.backtest.translator import _Context  # pyright: ignore[reportPrivateUsage]

# ---- in-memory simulation state --------------------------------------------


@dataclass
class _LivePhantom:
    """A skipped signal's phantom trade, mid-simulation. Until `position`
    is filled (at `signal_bar + 1`'s open) only `signal_bar` is known.
    """

    signal_bar: int
    position: _Position | None
    pending_exit_bar: int | None


@dataclass
class _LiveSim:
    """The shadow Tier-3 simulation's mutable in-memory state — the working
    form of a `Tier3State` for the duration of one `run_live_cycle` call.
    """

    history: TradeHistory
    signal_history: SignalHistory
    position: _Position | None
    pending_entry_bar: int | None
    pending_exit_bar: int | None
    phantoms: list[_LivePhantom]
    trade_id: int
    cash: float


@dataclass(frozen=True)
class _Compiled:
    """Per-cycle compiled spec — price arrays + the reused evaluators."""

    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    index: pd.DatetimeIndex
    entry_eval: Any
    raw_signal: list[bool] | None  # None for non-prior_signal Tier-3 specs
    cond_exits: list[_ExitEval]
    stop_method: StopLossMethod | None
    tp_method: TakeProfitMethod | None
    max_bars: int | None
    commission: float
    slippage: float
    sizing: PositionSizing
    atr: list[float] | None


# ---- Tier3State <-> in-memory conversion -----------------------------------


def _position_to_model(pos: _Position) -> Tier3ShadowPosition:
    return Tier3ShadowPosition(
        entry_bar=pos.entry_bar,
        entry_fill=pos.entry_fill,
        size=pos.size,
        stop_level=pos.stop_level,
        tp_level=pos.tp_level,
        trail_anchor=pos.trail_anchor,
    )


def _position_from_model(m: Tier3ShadowPosition) -> _Position:
    # `size` is persisted and restored exactly — a reloaded position must
    # produce a bit-identical return_pct (see Tier3ShadowPosition's docstring).
    return _Position(
        entry_bar=m.entry_bar,
        entry_fill=m.entry_fill,
        size=m.size,
        stop_level=m.stop_level,
        tp_level=m.tp_level,
        trail_anchor=m.trail_anchor,
    )


def _to_sim(tier3: Tier3State | None) -> _LiveSim:
    """Build the mutable sim state from a persisted checkpoint. A cold
    start (`tier3 is None`) uses a default `Tier3State` — so the initial
    `cash` has a single source, the model default.
    """
    if tier3 is None:
        tier3 = Tier3State()
    history = TradeHistory(
        trades=[
            CompletedTrade(
                trade_id=i + 1,
                entry_index=t.entry_index,
                exit_index=t.exit_index,
                entry_price=0.0,
                exit_price=0.0,
                pnl=0.0,
                return_pct=t.return_pct,
                outcome=TradeOutcome(t.outcome),
            )
            for i, t in enumerate(tier3.trade_history)
        ],
    )
    signal_history = SignalHistory(
        signals=[
            SignalRecord(
                signal_bar=s.signal_bar,
                fired=s.fired,
                return_pct=s.return_pct,
                outcome=TradeOutcome(s.outcome) if s.outcome is not None else None,
                resolved_bar=s.resolved_bar,
            )
            for s in tier3.signal_history
        ],
    )
    phantoms = [
        _LivePhantom(
            signal_bar=p.signal_bar,
            position=(
                _position_from_model(p.position) if p.position is not None else None
            ),
            pending_exit_bar=p.pending_exit_bar,
        )
        for p in tier3.pending_phantoms
    ]
    position = (
        _position_from_model(tier3.shadow_position)
        if tier3.shadow_position is not None
        else None
    )
    return _LiveSim(
        history=history,
        signal_history=signal_history,
        position=position,
        pending_entry_bar=tier3.pending_entry_bar,
        pending_exit_bar=tier3.pending_exit_bar,
        phantoms=phantoms,
        trade_id=tier3.trade_id,
        cash=tier3.cash,
    )


def _to_tier3(sim: _LiveSim, last_bar: int) -> Tier3State:
    """Serialise the mutable sim state back into a persistable checkpoint,
    stamped as-of `last_bar` (the last bar processed).
    """
    return Tier3State(
        last_bar=last_bar,
        signal_history=[
            Tier3SignalRecord(
                signal_bar=s.signal_bar,
                fired=s.fired,
                return_pct=s.return_pct,
                outcome=s.outcome.value if s.outcome is not None else None,
                resolved_bar=s.resolved_bar,
            )
            for s in sim.signal_history.signals
        ],
        trade_history=[
            Tier3CompletedTrade(
                entry_index=t.entry_index,
                exit_index=t.exit_index,
                return_pct=t.return_pct,
                outcome=t.outcome.value,
            )
            for t in sim.history.trades
        ],
        shadow_position=(
            _position_to_model(sim.position) if sim.position is not None else None
        ),
        pending_entry_bar=sim.pending_entry_bar,
        pending_exit_bar=sim.pending_exit_bar,
        pending_phantoms=[
            Tier3PendingPhantom(
                signal_bar=p.signal_bar,
                position=(
                    _position_to_model(p.position) if p.position is not None else None
                ),
                pending_exit_bar=p.pending_exit_bar,
            )
            for p in sim.phantoms
        ],
        trade_id=sim.trade_id,
        cash=sim.cash,
    )


# ---- the incremental phantom -----------------------------------------------


def _advance_phantom(ph: _LivePhantom, bar: int, c: _Compiled) -> tuple[float, int] | None:
    """Advance one pending phantom by one bar — `_phantom`'s loop body,
    stepped. Returns `(return_pct, resolved_bar)` when the phantom resolves,
    or None while it is still open.
    """
    if ph.position is None:
        # First advancement: fill at signal_bar + 1's open (this bar).
        raw_fill = c.opens[bar]
        if raw_fill != raw_fill or raw_fill <= 0.0:  # NaN / non-positive
            return 0.0, bar
        entry_fill = raw_fill * (1.0 + c.slippage)
        stop_level = _stop_level(c.stop_method, entry_fill, c.atr, ph.signal_bar)
        ph.position = _Position(
            entry_bar=ph.signal_bar,
            entry_fill=entry_fill,
            size=1.0,
            stop_level=stop_level,
            tp_level=_tp_level(c.tp_method, entry_fill, stop_level, c.atr, ph.signal_bar),
            trail_anchor=entry_fill,
        )
    pos = ph.position
    # A signal/time exit decided last bar fills at this open.
    if ph.pending_exit_bar is not None:
        return _phantom_return(pos.entry_fill, c.opens[bar], c.commission, c.slippage), bar
    # Intrabar stop / take-profit.
    hit_price, _reason = _intrabar_exit(pos, c.lows[bar], c.highs[bar], c.opens[bar])
    if hit_price is not None:
        return _phantom_return(pos.entry_fill, hit_price, c.commission, c.slippage), bar
    _update_trailing(pos, c.stop_method, c.highs[bar], c.atr, bar)
    # Signal / time exit decided at this close — NO `bar < n-1` guard (the
    # latest bar is not end-of-data; the exit fills next cycle).
    bars_held = bar - ph.signal_bar
    time_hit = c.max_bars is not None and bars_held >= c.max_bars
    cond_hit = any(ev(bar, ph.signal_bar) for ev in c.cond_exits)
    if time_hit or cond_hit:
        ph.pending_exit_bar = bar
    return None


# ---- the per-bar step ------------------------------------------------------


def _step(sim: _LiveSim, bar: int, c: _Compiled) -> None:
    """Advance the shadow simulation by one bar — `run_iterative_backtest`'s
    STEP 1/2/4/5 loop body, minus the equity mark and minus the `bar < n-1`
    guard, plus the incremental-phantom advancement.
    """
    uses_prior_signal = c.raw_signal is not None

    # STEP 1 — execute next-open fills decided on the previous bar.
    if sim.pending_entry_bar is not None and sim.position is None:
        fill = c.opens[bar]
        if fill == fill:  # not NaN
            sim.position = _open_position(
                sim.pending_entry_bar, fill, sim.cash, c.commission,
                c.slippage, c.sizing, c.stop_method, c.tp_method, c.atr,
            )
            if sim.position is not None:
                sim.cash -= sim.position.size * sim.position.entry_fill * (1.0 + c.commission)
        sim.pending_entry_bar = None
    if sim.pending_exit_bar is not None and sim.position is not None:
        fill = c.opens[bar]
        if fill == fill:
            sim.trade_id += 1
            sim.cash, trade = _close_position(
                sim.position, sim.pending_exit_bar, fill, c.commission,
                c.slippage, c.index, "signal", sim.trade_id, sim.cash,
            )
            sim.history.record(_completed(trade, sim.position, sim.trade_id))
            if uses_prior_signal:
                sim.signal_history.resolve(
                    sim.position.entry_bar, trade.return_pct, resolved_bar=bar,
                )
            sim.position = None
        sim.pending_exit_bar = None

    # STEP 2 — intrabar stop-loss / take-profit for an open position.
    if sim.position is not None:
        hit_price, reason = _intrabar_exit(
            sim.position, c.lows[bar], c.highs[bar], c.opens[bar],
        )
        if hit_price is not None:
            sim.trade_id += 1
            sim.cash, trade = _close_position(
                sim.position, bar, hit_price, c.commission, c.slippage,
                c.index, reason, sim.trade_id, sim.cash,
            )
            sim.history.record(_completed(trade, sim.position, sim.trade_id))
            if uses_prior_signal:
                sim.signal_history.resolve(
                    sim.position.entry_bar, trade.return_pct, resolved_bar=bar,
                )
            sim.position = None
        else:
            _update_trailing(sim.position, c.stop_method, c.highs[bar], c.atr, bar)

    # STEP 2.5 — advance every pending phantom by this bar; resolve any that
    # exited. Phantoms created in STEP 4 of THIS bar are advanced from the
    # next bar (this runs before STEP 4).
    for ph in list(sim.phantoms):
        resolved = _advance_phantom(ph, bar, c)
        if resolved is not None:
            return_pct, resolved_bar = resolved
            sim.signal_history.resolve(ph.signal_bar, return_pct, resolved_bar)
            sim.phantoms.remove(ph)

    # STEP 4 — evaluate the entry condition; act on it only when flat.
    fired = c.entry_eval(bar, sim.history, sim.signal_history)
    if sim.position is None and sim.pending_entry_bar is None:
        if c.raw_signal is None:
            if fired:
                sim.pending_entry_bar = bar
        elif c.raw_signal[bar]:
            # A raw signal fired while flat. `fired` folds in the gate.
            if fired:
                sim.pending_entry_bar = bar
                sim.signal_history.record_fired(signal_bar=bar)
            else:
                sim.signal_history.record_skipped_pending(signal_bar=bar)
                sim.phantoms.append(
                    _LivePhantom(signal_bar=bar, position=None, pending_exit_bar=None),
                )

    # STEP 5 — signal / time exits, decided at close, filled next open. NO
    # `bar < n-1` guard — the latest bar pends to the next cycle.
    if sim.position is not None and sim.pending_exit_bar is None:
        bars_held = bar - sim.position.entry_bar
        time_hit = c.max_bars is not None and bars_held >= c.max_bars
        cond_hit = any(ev(bar, sim.position.entry_bar) for ev in c.cond_exits)
        if time_hit or cond_hit:
            sim.pending_exit_bar = bar


# ---- public entry point ----------------------------------------------------


def run_live_cycle(
    spec: StrategySpec,
    data: dict[Timeframe, pd.DataFrame],
    prior_tier3: Tier3State | None,
    last_bar: int,
) -> tuple[Tier3State, SignalKind]:
    """Advance the live Tier-3 shadow simulation from a checkpoint.

    `data` is the full candle history (bar indices are absolute and stable
    across cycles). `prior_tier3` is the persisted checkpoint as of bar
    `last_bar` (pass `prior_tier3=None, last_bar=-1` for a cold start). The
    stepper processes every bar in `(last_bar, n)` and returns the advanced
    `Tier3State` plus the shadow simulation's decision on the latest bar:
    BUY when it opened an entry, EXIT when it decided to close, else HOLD.
    """
    if spec.direction is not Direction.LONG:
        raise IterativeBacktestError(
            f"the live Tier-3 stepper is long-only; got {spec.direction.value}",
        )
    if spec.filters:
        raise IterativeBacktestError(
            "entry filters are not supported by the live Tier-3 stepper",
        )
    primary_df = data[spec.primary_timeframe]
    index = primary_df.index
    if not isinstance(index, pd.DatetimeIndex):
        raise IterativeBacktestError("primary OHLCV index must be a DatetimeIndex")
    ctx = _Context(spec=spec, data=data, primary_index=index)
    n = len(primary_df)

    commission, slippage = _resolve_costs(spec)
    cond_exits, stop_method, tp_method, max_bars = _compile_exit_rules(spec.exit, ctx)
    compiled = _Compiled(
        opens=[float(v) for v in primary_df["open"].to_numpy()],
        highs=[float(v) for v in primary_df["high"].to_numpy()],
        lows=[float(v) for v in primary_df["low"].to_numpy()],
        closes=[float(v) for v in primary_df["close"].to_numpy()],
        index=index,
        entry_eval=_build_entry_evaluator(spec.entry.condition, ctx),
        raw_signal=(
            _build_raw_signal(spec.entry.condition, ctx)
            if condition_uses_prior_signal(spec.entry.condition)
            else None
        ),
        cond_exits=cond_exits,
        stop_method=stop_method,
        tp_method=tp_method,
        max_bars=max_bars,
        commission=commission,
        slippage=slippage,
        sizing=spec.position_sizing,
        atr=_atr_for_stop(stop_method, primary_df, tp_method),
    )

    sim = _to_sim(prior_tier3)
    for bar in range(last_bar + 1, n):
        _step(sim, bar, compiled)

    latest = n - 1
    if sim.pending_entry_bar == latest:
        decision = SignalKind.BUY
    elif sim.pending_exit_bar == latest:
        decision = SignalKind.EXIT
    else:
        decision = SignalKind.HOLD
    return _to_tier3(sim, latest), decision


__all__ = ["run_live_cycle"]
