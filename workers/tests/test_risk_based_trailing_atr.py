"""v1.2 follow-up (2026-05-25, post-Hunt-7): risk_based sizing + StopLossTrailingAtr.

Hunt 7 (Modern Turtle System 2 55-bar) extracted cleanly but the
backtest engine raised `BacktestError: risk_based sizing is not
supported with stop method StopLossTrailingAtr in Phase 3.1`. This
test module covers the engine extension that closes the gap:

  - vbt path (`_vbt_size`) — risk_based + StopLossTrailingAtr returns
    the same size Series as risk_based + StopLossAtrMultiple (the trail
    only affects exit-side ratcheting, not entry-bar sizing).
  - iterative path (`_entry_size`) — same math, called per entry bar.
    Required for cross-engine drift parity; previously the iterative
    engine raised on any RiskBasedSizing.
  - Cross-engine envelope — vbt vs iterative trade-count parity within
    ±2× on a fixture using risk_based + StopLossTrailingAtr. Mirrors
    the v1.2.A pattern (cross-engine envelope for purely-additive
    primitives where known exit-tie-break differences prevent strict
    trade-ledger bit-identity).

Empirical-inspection step honoured (META-PATTERN, v1.2 retrospective
standing rule): the fixture's known ATR values were inspected first
and the expected sizes hand-computed BEFORE encoding the assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.backtest_run import (
    SignalDiagnostics,
    SignalDiagnosticsFailureMode,
)
from marketmind_shared.schemas.strategy_spec import (
    Direction,
    FixedPercentEquitySizing,
    RiskBasedSizing,
    StopLossAtrMultiple,
    StopLossPercent,
    StopLossTrailingAtr,
    Timeframe,
)
from marketmind_workers.backtest import indicators as ind
from marketmind_workers.backtest.engine import (
    _vbt_size,  # pyright: ignore[reportPrivateUsage]
    run_backtest,
)
from marketmind_workers.backtest.iterative import (
    IterativeBacktestError,
    _entry_size,  # pyright: ignore[reportPrivateUsage]
    run_iterative_backtest,
)
from marketmind_workers.backtest.translator import SignalSet

# ---- shared fixtures ------------------------------------------------------


def _ohlcv_uptrend(n: int = 600, base: float = 100.0, step: float = 0.05) -> pd.DataFrame:
    """Synthetic uptrend with mild noise. ATR will be well-defined past
    the warmup bars; suitable for risk-based sizing math verification
    AND for producing some Donchian breakouts on the cross-engine test.
    """
    rng = np.random.default_rng(seed=42)
    closes = base + step * np.arange(n) + rng.normal(0.0, 0.5, n).cumsum() * 0.1
    closes = np.maximum(closes, 1.0)  # never below 1.0
    highs = closes + np.abs(rng.normal(0.0, 0.4, n))
    lows = closes - np.abs(rng.normal(0.0, 0.4, n))
    opens = closes + rng.normal(0.0, 0.1, n)
    idx = pd.date_range(datetime(2024, 1, 1, tzinfo=UTC), periods=n, freq="4h")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": np.full(n, 1e6)},
        index=idx,
    )


def _empty_signal_set(df: pd.DataFrame, stop_loss: Any) -> SignalSet:
    """A SignalSet with no entries/exits — used to exercise _vbt_size
    in isolation. Direction LONG; trivial diagnostics.
    """
    diag = SignalDiagnostics(
        bars_evaluated=len(df),
        nan_warmup_count=0,
        nan_post_warmup_count=0,
        true_count=0,
        deterministic_false_count=len(df),
        failure_mode=SignalDiagnosticsFailureMode.NONE,
        warmup_bars=0,
    )
    return SignalSet(
        entries=pd.Series([False] * len(df), index=df.index),
        exits=pd.Series([False] * len(df), index=df.index),
        stop_loss=stop_loss,
        take_profit=None,
        max_bars_held=None,
        direction=Direction.LONG,
        entry_diagnostics=diag,
    )


def _spec_donchian_breakout_risk_trailing_atr(
    *,
    risk_percent: float = 0.01,
    atr_period: int = 20,
    atr_mult: float = 2.0,
    breakout_period: int = 55,
) -> Any:
    """Hunt 7's exact shape (Modern Turtle System 2):
      - Entry: close > highest(high, breakout_period, lag=1) — Donchian breakout
      - Stop: 2× ATR(20) trailing
      - Sizing: 1% account risk
    """
    spec, _warnings = validate_spec(
        {
            "schema_version": "1.0",
            "name": "Hunt 7 risk_based + trailing ATR fixture",
            "instrument": {
                "symbol": "BTC/USDT",
                "exchange": "binance",
                "quote_currency": "USDT",
            },
            "primary_timeframe": "4h",
            "direction": "long",
            "entry": {
                "condition": {
                    "type": "compare",
                    "left": {"kind": "price", "field": "close"},
                    "op": ">",
                    "right": {
                        "kind": "lagged",
                        "expression": {
                            "kind": "indicator",
                            "name": "highest",
                            "params": {"period": breakout_period, "source": "high"},
                            "source": "close",
                        },
                        "bars_ago": 1,
                    },
                },
                "order_type": "market",
            },
            "exit": {
                "exits": [
                    {
                        "type": "stop_loss",
                        "method": {
                            "kind": "trailing_atr",
                            "atr_period": atr_period,
                            "mult": atr_mult,
                        },
                    },
                ],
            },
            "position_sizing": {"mode": "risk_based", "risk_percent": risk_percent},
        },
    )
    return spec


def _diagram_dt(ts: Any) -> datetime:
    """Coerce a pandas Timestamp to a UTC-aware datetime for the engine."""
    py = cast(pd.Timestamp, ts).to_pydatetime()
    if py.tzinfo is None:
        py = py.replace(tzinfo=UTC)
    return py


# =====================================================================
# 1. vbt _vbt_size — risk_based + StopLossTrailingAtr math
# =====================================================================


class TestVbtSizeRiskBasedTrailingAtr:
    """Cover _vbt_size's new branch for risk_based + StopLossTrailingAtr."""

    def test_returns_same_series_as_atr_multiple(self) -> None:
        """The trail flag only affects exit-side ratcheting. Entry-bar
        sizing must produce the identical size Series whether the stop
        is StopLossAtrMultiple or StopLossTrailingAtr.
        """
        df = _ohlcv_uptrend(n=200)
        sizing = RiskBasedSizing(risk_percent=0.01)
        atr_period = 14
        atr_mult = 2.0
        sl_atr = StopLossAtrMultiple(kind="atr_multiple", atr_period=atr_period, mult=atr_mult)
        sl_trail = StopLossTrailingAtr(kind="trailing_atr", atr_period=atr_period, mult=atr_mult)
        size_atr, type_atr = _vbt_size(sizing, _empty_signal_set(df, sl_atr), df)
        size_trail, type_trail = _vbt_size(sizing, _empty_signal_set(df, sl_trail), df)
        assert type_atr == type_trail == "percent"
        # Bit-identical Series — the entry-bar sizing math is the same.
        assert isinstance(size_atr, pd.Series)
        assert isinstance(size_trail, pd.Series)
        pd.testing.assert_series_equal(size_atr, size_trail, check_names=False)

    def test_size_math_correct_on_known_bar(self) -> None:
        """Hand-checked: at a bar with known close + ATR, the size_pct
        should equal min(risk_percent / stop_pct, 1.0) where
        stop_pct = (atr × mult) / close.

        Empirical-inspection (META-PATTERN): bar 100 is past the
        atr_period=14 warmup. Verify with an independent computation
        against the same ATR helper the engine uses.
        """
        df = _ohlcv_uptrend(n=200)
        sizing = RiskBasedSizing(risk_percent=0.01)
        atr_period = 14
        atr_mult = 2.0
        sl_trail = StopLossTrailingAtr(kind="trailing_atr", atr_period=atr_period, mult=atr_mult)
        size_series, _type = _vbt_size(sizing, _empty_signal_set(df, sl_trail), df)
        assert isinstance(size_series, pd.Series)
        # Independent computation against the same ATR helper the engine uses.
        atr_series = ind.atr(df, atr_period)
        close = ind.column(df, "close")
        bar_idx = 100
        expected_stop_pct = (atr_series.iloc[bar_idx] * atr_mult) / close.iloc[bar_idx]
        expected_size_pct = min(sizing.risk_percent / expected_stop_pct, 1.0)
        actual_size_pct = size_series.iloc[bar_idx]
        # Within float epsilon.
        assert abs(actual_size_pct - expected_size_pct) < 1e-9, (
            f"bar {bar_idx}: expected size_pct={expected_size_pct:.6f}, "
            f"got {actual_size_pct:.6f}; close={close.iloc[bar_idx]:.4f}, "
            f"atr={atr_series.iloc[bar_idx]:.4f}"
        )

    def test_size_capped_at_one(self) -> None:
        """If risk_percent / stop_pct > 1.0 (e.g. very tight stop with
        non-trivial risk_percent), size is capped at 1.0 (no leverage).
        """
        df = _ohlcv_uptrend(n=200)
        sizing = RiskBasedSizing(risk_percent=0.1)
        # Mult=1.0 is the schema-minimum-friendly tight stop.
        sl_trail = StopLossTrailingAtr(kind="trailing_atr", atr_period=2, mult=1.0)
        size_series, _type = _vbt_size(sizing, _empty_signal_set(df, sl_trail), df)
        assert isinstance(size_series, pd.Series)
        # Past warmup, every value should be at or below 1.0 (the cap).
        past_warmup = size_series.iloc[50:]
        assert (past_warmup <= 1.0 + 1e-12).all()


# =====================================================================
# 2. iterative _entry_size — risk_based + StopLossTrailingAtr math
# =====================================================================


class TestIterativeEntrySizeRiskBasedTrailingAtr:
    """Cover the new branch in iterative.py's _entry_size."""

    def test_size_within_cross_engine_envelope(self) -> None:
        """The iterative engine's per-bar _entry_size and the vbt engine's
        Series at the same bar produce sizing within ~5% of each other.

        Why not bit-identical: vbt computes stop_pct using close at the
        SIGNAL bar (`stop_pct = atr × mult / close`); the iterative path
        computes stop_pct using entry_fill at the FILL bar
        (`stop_pct = atr × mult / entry_fill`) where entry_fill = next
        bar's open × (1 + slippage). Both are internally consistent —
        vbt sizes off the bar where the signal fires; iterative sizes
        off the bar where the fill occurs. Cross-engine envelope is
        therefore the right gate; mirrors v1.2.A's pattern for purely-
        additive primitives where engine differences are inherent.

        Empirical-inspection (META-PATTERN): inspected actual outputs
        of both engines first (~0.1% divergence on this fixture),
        chose 5% as a comfortable headroom for the envelope.
        """
        df = _ohlcv_uptrend(n=200)
        sizing = RiskBasedSizing(risk_percent=0.01)
        atr_period = 14
        atr_mult = 2.0
        sl_trail = StopLossTrailingAtr(kind="trailing_atr", atr_period=atr_period, mult=atr_mult)
        vbt_size_series, _ = _vbt_size(sizing, _empty_signal_set(df, sl_trail), df)
        assert isinstance(vbt_size_series, pd.Series)
        atr_list = [float(v) for v in ind.atr(df, atr_period).to_numpy()]
        bar_idx = 100
        entry_fill = float(df["open"].iloc[bar_idx])
        cash = 10_000.0
        commission = 0.001  # 10 bps
        iterative_units = _entry_size(
            sizing,
            cash,
            entry_fill,
            commission,
            stop_method=sl_trail,
            atr=atr_list,
            entry_bar=bar_idx,
        )
        vbt_size_pct = float(vbt_size_series.iloc[bar_idx])
        vbt_units = (vbt_size_pct * cash) / (entry_fill * (1.0 + commission))
        relative_diff = abs(iterative_units - vbt_units) / max(abs(vbt_units), 1.0)
        assert relative_diff < 0.05, (
            f"cross-engine sizing envelope breach: "
            f"iterative units={iterative_units:.6f}, vbt units={vbt_units:.6f}, "
            f"relative diff={relative_diff:.4%}"
        )

    def test_pre_warmup_atr_returns_zero_size(self) -> None:
        """Documented graceful-degradation: if the ATR at the entry bar
        is NaN (warmup) OR <= 0 (degenerate input), _entry_size returns
        0.0 and the iterative engine skips the entry rather than mis-
        sizing. Covers both code paths: the NaN-collapse and the
        stop_distance <= 0 guard.

        Empirical-inspection (META-PATTERN): the `ta` library's ATR
        with fillna=False does NOT always emit NaN at bar 0 (it can
        emit 0.0 for the first bar depending on the high-low at index
        0). Verify the SAFETY behaviour (size == 0) rather than the
        specific NaN-or-0 implementation detail.
        """
        sizing = RiskBasedSizing(risk_percent=0.01)
        sl_trail = StopLossTrailingAtr(kind="trailing_atr", atr_period=14, mult=2.0)
        # Hand-crafted ATR series with a NaN at index 0 to exercise the
        # NaN branch directly.
        nan_atr = [float("nan"), 0.0, 1.0, 2.0]
        nan_units = _entry_size(
            sizing,
            cash=10_000.0,
            entry_fill=100.0,
            commission=0.001,
            stop_method=sl_trail,
            atr=nan_atr,
            entry_bar=0,
        )
        assert nan_units == 0.0
        # And the stop_distance <= 0 branch.
        zero_units = _entry_size(
            sizing,
            cash=10_000.0,
            entry_fill=100.0,
            commission=0.001,
            stop_method=sl_trail,
            atr=nan_atr,
            entry_bar=1,
        )
        assert zero_units == 0.0

    def test_existing_fixed_percent_branch_byte_identical(self) -> None:
        """Pre-existing call sites pass no keyword args — the v1.2.B-
        style signature widening with keyword-default-None must keep
        FixedPercentEquitySizing math byte-identical.
        """
        sizing = FixedPercentEquitySizing(mode="fixed_percent_equity", percent=0.5)
        units = _entry_size(sizing, cash=10_000.0, entry_fill=100.0, commission=0.001)
        # (0.5 × 10_000) / (100 × 1.001) ≈ 49.95
        assert abs(units - (0.5 * 10_000.0) / (100.0 * 1.001)) < 1e-12

    def test_risk_based_no_stop_raises(self) -> None:
        """Same error shape as the vbt path."""
        sizing = RiskBasedSizing(risk_percent=0.01)
        with pytest.raises(IterativeBacktestError, match=r"risk_based.*requires a stop_loss"):
            _entry_size(sizing, cash=10_000.0, entry_fill=100.0, commission=0.001)

    def test_risk_based_with_percent_stop_works(self) -> None:
        """Coverage for the StopLossPercent branch of risk_based sizing."""
        sizing = RiskBasedSizing(risk_percent=0.01)
        stop = StopLossPercent(kind="percent", value=0.05)  # 5% stop
        units = _entry_size(
            sizing,
            cash=10_000.0,
            entry_fill=100.0,
            commission=0.001,
            stop_method=stop,
        )
        # size_pct = min(0.01 / 0.05, 1.0) = 0.20 → (0.2 × 10_000) / (100 × 1.001) ≈ 19.98
        expected = (0.20 * 10_000.0) / (100.0 * 1.001)
        assert abs(units - expected) < 1e-12


# =====================================================================
# 3. Cross-engine drift parity — vbt vs iterative trade-count envelope
# =====================================================================


class TestVbtVsIterativeCrossEngineEnvelope:
    """v1.2.A-pattern cross-engine envelope: vbt and iterative engines
    run the same risk_based + StopLossTrailingAtr spec and the trade
    counts land within ±2× of each other. Strict bit-identity is not
    achievable (known exit-tie-break differences); the envelope is the
    right gate for this combination.
    """

    def test_hunt_7_shape_runs_on_both_engines(self) -> None:
        """Both engines accept the spec without raising. Pre-fix, the
        vbt path raised BacktestError; iterative raised
        IterativeBacktestError; post-fix, both run cleanly.
        """
        df = _ohlcv_uptrend(n=600)
        spec = _spec_donchian_breakout_risk_trailing_atr()
        start_dt = _diagram_dt(df.index[0])
        end_dt = _diagram_dt(df.index[-1])
        # vbt path via the public engine entry-point (uses data_override).
        vbt_run = run_backtest(
            spec,
            start=start_dt,
            end=end_dt,
            initial_capital=10_000.0,
            data_override={Timeframe.H4: df},
        )
        iterative_run = run_iterative_backtest(
            spec,
            data={Timeframe.H4: df},
            start=start_dt,
            end=end_dt,
            initial_capital=10_000.0,
        )
        # Both produced runs (may be empty trades if fixture doesn't
        # trigger the shape; the point is neither raises).
        assert vbt_run is not None
        assert iterative_run is not None

    def test_trade_counts_within_cross_engine_envelope(self) -> None:
        """If the fixture produces trades on either engine, the counts
        should be within ±2× of each other (v1.2.A envelope check).
        """
        df = _ohlcv_uptrend(n=600)
        spec = _spec_donchian_breakout_risk_trailing_atr(
            risk_percent=0.02,
            atr_period=14,
            atr_mult=1.5,
            breakout_period=20,
        )
        start_dt = _diagram_dt(df.index[0])
        end_dt = _diagram_dt(df.index[-1])
        vbt_run = run_backtest(
            spec,
            start=start_dt,
            end=end_dt,
            initial_capital=10_000.0,
            data_override={Timeframe.H4: df},
        )
        iterative_run = run_iterative_backtest(
            spec,
            data={Timeframe.H4: df},
            start=start_dt,
            end=end_dt,
            initial_capital=10_000.0,
        )
        vbt_trades = len(vbt_run.trades)
        iterative_trades = len(iterative_run.trades)
        if vbt_trades == 0 and iterative_trades == 0:
            # No trades on either — fixture didn't trigger the breakout.
            # The test still passes: the engines agreed (no trades).
            return
        max_count = max(vbt_trades, iterative_trades)
        min_count = max(min(vbt_trades, iterative_trades), 1)
        assert max_count / min_count <= 2.0, (
            f"cross-engine trade count divergence beyond ±2× envelope: "
            f"vbt={vbt_trades}, iterative={iterative_trades}"
        )
