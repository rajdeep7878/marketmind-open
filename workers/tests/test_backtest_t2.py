"""A.3a — Tier-2 (unbounded, input-dependent stateful) backtest tests.

T2 conditions track state across the whole bar series but depend only
on price/indicator inputs — no trade outcomes. A.3a evaluates them
vectorised (cummax/cummin for ratchet, a ffill latch for regime_state),
no per-bar scan. This module verifies the latch and ratchet semantics
on hand-built series, and that Tier-3 specs are refused cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import SignalDiagnosticsFailureMode, validate_spec
from marketmind_shared.schemas.strategy_spec import Timeframe
from marketmind_workers.backtest.translator import TranslationError, build_signals

_PRICE_CLOSE = {"kind": "price", "field": "close"}


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="4h")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": np.full(n, 1e6)},
        index=idx,
    )


def _spec(entry_condition: dict[str, Any], *, schema_version: str = "2.0") -> Any:
    spec, _warnings = validate_spec(
        {
            "schema_version": schema_version,
            "name": "T2 Test",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {"condition": entry_condition, "order_type": "market"},
            "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
        },
    )
    return spec


def _crossover_const(value: float, direction: str) -> dict[str, Any]:
    return {
        "type": "crossover",
        "series": _PRICE_CLOSE,
        "threshold": {"kind": "constant", "value": value},
        "direction": direction,
    }


# ---- regime_state latch ---------------------------------------------------


def test_regime_state_latches_on_and_off() -> None:
    # closes cross 150 up (bar 2), down (bar 5), up again (bar 9).
    closes = [140.0, 145.0, 155.0, 160.0, 165.0, 145.0, 140.0, 135.0, 145.0, 155.0, 160.0]
    regime = {
        "type": "regime_state",
        "enter_when": _crossover_const(150.0, "above"),
        "exit_when": _crossover_const(150.0, "below"),
        "initial": False,
    }
    entries = build_signals(_spec(regime), {Timeframe.H4: _ohlcv(closes)}).entries.to_numpy()
    # ON from the enter bar (2) until the exit bar (5) excl., then OFF,
    # then ON again from the re-enter bar (9).
    assert list(entries) == [
        False, False, True, True, True, False, False, False, False, True, True,
    ]


def test_regime_state_initial_true_holds_until_first_exit() -> None:
    closes = [160.0, 165.0, 170.0, 145.0, 140.0, 135.0]
    regime = {
        "type": "regime_state",
        "enter_when": _crossover_const(150.0, "above"),
        "exit_when": _crossover_const(150.0, "below"),
        "initial": True,
    }
    entries = build_signals(_spec(regime), {Timeframe.H4: _ohlcv(closes)}).entries.to_numpy()
    # initial=True -> ON from bar 0 until the cross-below at bar 3.
    assert list(entries) == [True, True, True, False, False, False]


# ---- ratchet (reset="never") ----------------------------------------------


def _ratchet(extremum: str) -> dict[str, Any]:
    return {"kind": "ratchet", "source": _PRICE_CLOSE, "extremum": extremum, "reset": "never"}


def test_ratchet_max_reset_never_tracks_running_high() -> None:
    # close >= running max(close) is True only at a fresh all-time high.
    closes = [100.0, 110.0, 105.0, 120.0, 115.0, 120.0, 130.0]
    cond = {"type": "compare", "left": _PRICE_CLOSE, "op": ">=", "right": _ratchet("max")}
    entries = build_signals(_spec(cond), {Timeframe.H4: _ohlcv(closes)}).entries.to_numpy()
    assert list(entries) == [True, True, False, True, False, True, True]


def test_ratchet_min_reset_never_tracks_running_low() -> None:
    closes = [100.0, 90.0, 95.0, 80.0, 85.0, 80.0, 70.0]
    cond = {"type": "compare", "left": _PRICE_CLOSE, "op": "<=", "right": _ratchet("min")}
    entries = build_signals(_spec(cond), {Timeframe.H4: _ohlcv(closes)}).entries.to_numpy()
    assert list(entries) == [True, True, False, True, False, True, True]


# ---- Tier-3 guard ---------------------------------------------------------


def test_tier3_prior_trade_condition_is_rejected() -> None:
    cond = {"type": "prior_trade", "predicate": "last_won", "n": 1}
    with pytest.raises(TranslationError, match="Tier-3"):
        build_signals(_spec(cond), {Timeframe.H4: _ohlcv([100.0] * 10)})


def test_tier3_per_trade_ratchet_is_rejected() -> None:
    cond = {
        "type": "compare",
        "left": _PRICE_CLOSE,
        "op": "<",
        "right": {"kind": "ratchet", "source": _PRICE_CLOSE, "extremum": "max", "reset": "per_trade"},
    }
    with pytest.raises(TranslationError, match="Tier-3"):
        build_signals(_spec(cond), {Timeframe.H4: _ohlcv([100.0] * 10)})


# ---- entry diagnostics for stateful conditions ----------------------------


def test_regime_state_entry_diagnostics_are_clean() -> None:
    # regime_state yields a clean bool series (no NaN), so the v1.1
    # diagnostics classifier reads it without special-casing.
    closes = [140.0, 145.0, 155.0, 160.0, 165.0, 145.0, 140.0, 135.0, 145.0, 155.0, 160.0]
    regime = {
        "type": "regime_state",
        "enter_when": _crossover_const(150.0, "above"),
        "exit_when": _crossover_const(150.0, "below"),
        "initial": False,
    }
    signals = build_signals(_spec(regime), {Timeframe.H4: _ohlcv(closes)})
    diag = signals.entry_diagnostics
    assert diag.failure_mode is SignalDiagnosticsFailureMode.NONE
    assert diag.bars_evaluated == 11
    assert diag.true_count == 5
    assert diag.nan_warmup_count == 0
    assert diag.nan_post_warmup_count == 0
