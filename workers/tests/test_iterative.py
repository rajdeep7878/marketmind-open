"""A.3b — Tier-3 behaviour tests for the iterative simulator.

Deterministic synthetic price series exercise the genuinely
outcome-dependent logic the vectorbt path cannot express:

  * skip-after-winner (Turtle System 1) — two winning breakouts, the
    second skipped because `prior_trade(last_won)` is True;
  * per-trade ratchet — the running extremum resets at each trade
    entry, so trade 2's trailing exit ignores trade 1's high;
  * the router — a Tier-3 spec reaches the iterative path via
    run_backtest, not just a direct call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import SignalDiagnosticsFailureMode, validate_spec
from marketmind_shared.schemas.strategy_spec import StrategySpec, Timeframe
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import IterativeBacktestError, run_iterative_backtest

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2024, 6, 1, tzinfo=UTC)
_PRICE_CLOSE: dict[str, Any] = {"kind": "price", "field": "close"}


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(_START, periods=n, freq="4h")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": np.full(n, 1e6)},
        index=idx,
    )


def _crossover(value: float, direction: str) -> dict[str, Any]:
    return {
        "type": "crossover",
        "series": _PRICE_CLOSE,
        "threshold": {"kind": "constant", "value": value},
        "direction": direction,
    }


def _validated(spec_dict: dict[str, Any]) -> StrategySpec:
    spec, _warnings = validate_spec(spec_dict)
    return spec


# ---- skip-after-winner -----------------------------------------------------

# Two breakouts above 110, each followed by a 4-bar rise — both winners
# absent any gate. The crossover fires at bar 5 and bar 15.
_SKIP_CLOSES = [
    100.0, 101.0, 102.0, 103.0, 104.0, 112.0, 115.0, 120.0, 125.0, 130.0,
    135.0, 108.0, 106.0, 104.0, 105.0, 113.0, 116.0, 122.0, 128.0, 134.0,
    140.0, 138.0, 136.0, 134.0, 132.0, 130.0, 128.0, 126.0, 124.0, 122.0,
]


def _breakout_spec(*, skip_after_winner: bool) -> StrategySpec:
    breakout = _crossover(110.0, "above")
    if skip_after_winner:
        entry_condition: dict[str, Any] = {
            "type": "and",
            "conditions": [
                breakout,
                {"type": "not", "condition": {
                    "type": "prior_trade", "predicate": "last_won", "n": 1}},
            ],
        }
        schema_version = "2.0"
    else:
        entry_condition = breakout
        schema_version = "1.0"
    return _validated(
        {
            "schema_version": schema_version,
            "name": "Turtle-style Breakout",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {"condition": entry_condition, "order_type": "market"},
            "exit": {"exits": [{"type": "time", "max_bars_held": 4}]},
            "position_sizing": {"mode": "fixed_quantity", "quantity": 0.5},
        },
    )


def test_skip_after_winner_skips_the_second_winning_breakout() -> None:
    data = {Timeframe.H4: _ohlcv(_SKIP_CLOSES)}

    no_skip = run_iterative_backtest(
        _breakout_spec(skip_after_winner=False), data, _START, _END, 10_000.0,
    )
    with_skip = run_iterative_backtest(
        _breakout_spec(skip_after_winner=True), data, _START, _END, 10_000.0,
    )

    # Without the gate: both breakouts trade, and both win — so the gate
    # has a winner to react to.
    assert len(no_skip.trades) == 2
    assert all(t.pnl > 0 for t in no_skip.trades)

    # With the gate: the second breakout is skipped because the first
    # trade won. Exactly one trade, and it is the first one.
    assert len(with_skip.trades) == 1
    assert with_skip.trades[0].entry_time == no_skip.trades[0].entry_time
    assert with_skip.trades[0].exit_time == no_skip.trades[0].exit_time


def test_router_sends_tier3_specs_to_the_iterative_path() -> None:
    # A prior_trade spec routed through run_backtest must reach the
    # iterative engine and produce the same ledger as a direct call.
    spec = _breakout_spec(skip_after_winner=True)
    data = {Timeframe.H4: _ohlcv(_SKIP_CLOSES)}
    routed = run_backtest(spec, _START, _END, 10_000.0, data_override=data)
    direct = run_iterative_backtest(spec, data, _START, _END, 10_000.0)
    assert len(routed.trades) == len(direct.trades) == 1
    assert routed.trades[0].entry_time == direct.trades[0].entry_time


def test_iterative_entry_diagnostics_are_populated_for_tier3() -> None:
    spec = _breakout_spec(skip_after_winner=True)
    data = {Timeframe.H4: _ohlcv(_SKIP_CLOSES)}
    run = run_iterative_backtest(spec, data, _START, _END, 10_000.0)
    diag = run.entry_diagnostics
    assert diag is not None
    assert diag.bars_evaluated == len(_SKIP_CLOSES)
    assert diag.true_count >= 1
    assert diag.failure_mode is SignalDiagnosticsFailureMode.NONE


# ---- per-trade ratchet -----------------------------------------------------

# Trade 1 rises to a high of 140; trade 2 peaks far lower at 118. A
# trailing exit "close < 0.9 * ratchet(max close since entry)" must use
# each trade's OWN high — if the ratchet failed to reset it would carry
# 140 and exit trade 2 on its very first bar.
_RATCHET_CLOSES = [
    100.0, 100.0, 100.0, 100.0, 110.0, 120.0, 130.0, 140.0, 135.0, 124.0,
    104.0, 100.0, 98.0, 102.0, 112.0, 115.0, 118.0, 116.0, 110.0, 105.0,
    100.0, 98.0, 96.0, 94.0, 92.0,
]


def _ratchet_spec() -> StrategySpec:
    trailing_exit: dict[str, Any] = {
        "type": "compare",
        "left": _PRICE_CLOSE,
        "op": "<",
        "right": {
            "kind": "scaled",
            "factor": 0.9,
            "expression": {
                "kind": "ratchet", "source": _PRICE_CLOSE,
                "extremum": "max", "reset": "per_trade",
            },
        },
    }
    return _validated(
        {
            "schema_version": "2.0",
            "name": "Per-trade Ratchet Trailing Exit",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {"condition": _crossover(105.0, "above"), "order_type": "market"},
            "exit": {"exits": [{"type": "condition", "condition": trailing_exit}]},
            "position_sizing": {"mode": "fixed_quantity", "quantity": 0.5},
        },
    )


def test_per_trade_ratchet_resets_at_each_trade_entry() -> None:
    data = {Timeframe.H4: _ohlcv(_RATCHET_CLOSES)}
    run = run_iterative_backtest(_ratchet_spec(), data, _START, _END, 10_000.0)
    index = data[Timeframe.H4].index

    assert len(run.trades) == 2
    # Trade 1 trails off its 140 high and exits at bar 9 (close 124 <
    # 0.9 * 140).
    assert run.trades[0].entry_time == index[4]
    assert run.trades[0].exit_time == index[9]
    # Trade 2's ratchet RESETS: its high is 118, so the exit fires only
    # at bar 19 (close 105 < 0.9 * 118 = 106.2). Had the ratchet carried
    # trade 1's 140, trade 2 would have exited at bar 15 on its first
    # bar (115 < 0.9 * 140 = 126).
    assert run.trades[1].entry_time == index[14]
    assert run.trades[1].exit_time == index[19]


# ---- prior_signal / phantom outcomes ---------------------------------------

# Three >110 breakouts — bars 5, 14, 21 — each held 4 bars by the time exit.
# Trade 1 wins (entry 118 -> exit 134), trade 2 loses (113 -> 98), trade 3
# wins (118 -> 134). The win/lose/win shape is what separates prior_signal
# from prior_trade — see test_prior_signal_breaks_the_skip_after_winner_latch.
_WLW_CLOSES = [
    100.0, 100.0, 100.0, 100.0, 100.0, 115.0, 118.0, 122.0, 126.0, 130.0,
    134.0, 104.0, 100.0, 100.0, 115.0, 113.0, 110.0, 106.0, 102.0, 98.0,
    100.0, 115.0, 118.0, 122.0, 126.0, 130.0, 134.0, 130.0, 128.0, 126.0,
]

_GATES: dict[str, dict[str, Any]] = {
    "prior_trade": {
        "type": "not",
        "condition": {"type": "prior_trade", "predicate": "last_won", "n": 1},
    },
    "prior_signal_won": {
        "type": "not",
        "condition": {"type": "prior_signal", "predicate": "last_would_have_won"},
    },
    "prior_signal_fired": {
        "type": "not",
        "condition": {"type": "prior_signal", "predicate": "last_fired"},
    },
}


def _gated_breakout_spec(gate: str | None) -> StrategySpec:
    """A >110 breakout with a 4-bar time exit, optionally AND-ed with a
    stateful gate. gate=None -> ungated (every breakout trades).
    """
    breakout = _crossover(110.0, "above")
    if gate is None:
        entry_condition: dict[str, Any] = breakout
        schema_version = "1.0"
    else:
        entry_condition = {"type": "and", "conditions": [breakout, _GATES[gate]]}
        schema_version = "2.0"
    return _validated(
        {
            "schema_version": schema_version,
            "name": f"WLW Breakout ({gate or 'ungated'})",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {"condition": entry_condition, "order_type": "market"},
            "exit": {"exits": [{"type": "time", "max_bars_held": 4}]},
            "position_sizing": {"mode": "fixed_quantity", "quantity": 0.5},
            "costs": {"commission_pct": 0.001, "slippage_pct": 0.0},
        },
    )


def test_prior_signal_breaks_the_skip_after_winner_latch() -> None:
    """The headline result. With win/lose/win breakouts, prior_trade
    latches shut after the first winner (1 trade) — a skipped breakout
    leaves no trade, so "the last trade" never advances. prior_signal,
    seeing the skipped middle breakout's phantom LOSS, re-opens the gate
    (2 trades). The ungated breakout takes all three.
    """
    data = {Timeframe.H4: _ohlcv(_WLW_CLOSES)}

    ungated = run_iterative_backtest(_gated_breakout_spec(None), data, _START, _END, 10_000.0)
    prior_trade = run_iterative_backtest(
        _gated_breakout_spec("prior_trade"), data, _START, _END, 10_000.0,
    )
    prior_signal = run_iterative_backtest(
        _gated_breakout_spec("prior_signal_won"), data, _START, _END, 10_000.0,
    )

    # Ungated: all three breakouts trade — win, loss, win.
    assert len(ungated.trades) == 3
    assert [t.pnl > 0 for t in ungated.trades] == [True, False, True]

    # prior_trade LATCHES on the first winner: just one trade.
    assert len(prior_trade.trades) == 1

    # prior_signal does NOT latch. Breakout 2 is skipped (breakout 1 would
    # have won), its phantom LOSES, so breakout 3 — whose most recent
    # signal now lost — fires. Two real trades: the 1st and 3rd breakouts.
    assert len(prior_signal.trades) == 2
    assert prior_signal.trades[0].entry_time == ungated.trades[0].entry_time
    assert prior_signal.trades[1].entry_time == ungated.trades[2].entry_time


def test_prior_signal_last_fired_alternates_take_and_skip() -> None:
    """`not(prior_signal last_fired)` takes a breakout only when the
    previous evaluated signal was skipped — it alternates fire / skip /
    fire independent of win or loss. On the three WLW breakouts that is
    two trades: the 1st and 3rd.
    """
    data = {Timeframe.H4: _ohlcv(_WLW_CLOSES)}
    run = run_iterative_backtest(
        _gated_breakout_spec("prior_signal_fired"), data, _START, _END, 10_000.0,
    )
    ungated = run_iterative_backtest(_gated_breakout_spec(None), data, _START, _END, 10_000.0)
    assert len(run.trades) == 2
    assert run.trades[0].entry_time == ungated.trades[0].entry_time
    assert run.trades[1].entry_time == ungated.trades[2].entry_time


def test_prior_signal_phantom_outcome_matches_the_real_trade() -> None:
    """Phantom-outcome correctness: the would-have-been return the
    simulator computes for a skipped signal equals — to 1e-9 — the return
    a real trade entered on that same signal produces. Checked directly
    against the phantom evaluator the simulator builds.
    """
    # White-box import: the phantom evaluator is the highest-risk new
    # arithmetic and is asserted here against a real trade's P&L.
    from marketmind_workers.backtest.iterative import (
        _build_phantom_evaluator,  # pyright: ignore[reportPrivateUsage]
    )

    data = {Timeframe.H4: _ohlcv(_WLW_CLOSES)}
    closes = list(_WLW_CLOSES)
    # _ohlcv sets open == close, high = close + 1, low = close - 1.
    opens = list(closes)
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    n = len(closes)

    # The WLW spec's only exit is a 4-bar time exit — no stop, TP, or
    # condition exit — so the phantom machinery is fully specified here.
    # Commission + slippage match the engine's defaults (FeeModel 10 bps,
    # SlippageModel 5 bps as of Phase B.2, 2026-05-23) so the phantom
    # math agrees with the real trade math run through the engine.
    phantom = _build_phantom_evaluator(
        opens, highs, lows, closes, n,
        commission=0.001, slippage=0.0005,
        stop_method=None, tp_method=None, max_bars=4, cond_exits=[], atr=None,
    )

    # Real trades from the ungated run — every breakout becomes a trade,
    # at signal bars 5, 14, 21 (the breakout bars of _WLW_CLOSES).
    ungated = run_iterative_backtest(_gated_breakout_spec(None), data, _START, _END, 10_000.0)
    assert len(ungated.trades) == 3
    for signal_bar, trade in zip((5, 14, 21), ungated.trades, strict=True):
        phantom_return, _resolved = phantom(signal_bar)
        assert phantom_return == pytest.approx(trade.return_pct, abs=1e-9)


def test_prior_signal_backtest_is_deterministic() -> None:
    # Phantom computation is pure arithmetic over the price arrays — two
    # runs of the same spec must be bit-identical.
    data = {Timeframe.H4: _ohlcv(_WLW_CLOSES)}
    spec = _gated_breakout_spec("prior_signal_won")
    first = run_iterative_backtest(spec, data, _START, _END, 10_000.0)
    second = run_iterative_backtest(spec, data, _START, _END, 10_000.0)
    assert [t.entry_time for t in first.trades] == [t.entry_time for t in second.trades]
    assert [t.exit_time for t in first.trades] == [t.exit_time for t in second.trades]
    assert [t.pnl for t in first.trades] == [t.pnl for t in second.trades]


def test_router_sends_prior_signal_specs_to_the_iterative_path() -> None:
    # prior_signal is Tier-3: run_backtest must route it to the iterative
    # engine and produce the same ledger as a direct call.
    spec = _gated_breakout_spec("prior_signal_won")
    data = {Timeframe.H4: _ohlcv(_WLW_CLOSES)}
    routed = run_backtest(spec, _START, _END, 10_000.0, data_override=data)
    direct = run_iterative_backtest(spec, data, _START, _END, 10_000.0)
    assert len(routed.trades) == len(direct.trades) == 2
    assert routed.trades[0].entry_time == direct.trades[0].entry_time


def test_prior_signal_entry_without_a_core_signal_raises() -> None:
    # A bare `not(prior_signal ...)` entry is a gate with no signal to
    # gate — the simulator cannot define "a signal", so it fails loudly
    # rather than silently mis-modelling.
    bad = _validated(
        {
            "schema_version": "2.0",
            "name": "Gate without a signal",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {
                "condition": {
                    "type": "not",
                    "condition": {"type": "prior_signal", "predicate": "last_would_have_won"},
                },
                "order_type": "market",
            },
            "exit": {"exits": [{"type": "time", "max_bars_held": 4}]},
            "position_sizing": {"mode": "fixed_quantity", "quantity": 0.5},
        },
    )
    data = {Timeframe.H4: _ohlcv(_WLW_CLOSES)}
    with pytest.raises(IterativeBacktestError, match="prior_signal entry must be"):
        run_iterative_backtest(bad, data, _START, _END, 10_000.0)
