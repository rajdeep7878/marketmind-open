"""v1.3 — RSICondition (Wilder's RSI oscillator mean-reversion gate) tests.

Four test classes mirroring v1.2.C (TimeOfDayCondition) structure:

  1. TestRSIConditionSchema — Pydantic shape: bounds, defaults, valid/
     invalid threshold + period, JSON round-trip, discriminator routing.

  2. TestEvalRSI — pure unit tests on the `_eval_rsi` helper, including
     the EMPIRICAL-INSPECTION test: a deterministic V-shape fixture
     whose RSI crossing of 30 was hand-verified by printing the series
     (see docstrings for the literal bar indices). The assertions encode
     HAND-VERIFIED literals, never the helper's own output.

  3. TestDispatcherIdentity — _eval_condition_on_tf routes an
     RSICondition to the same _eval_rsi helper that's importable
     directly. assert_series_equal proves a single implementation, so
     drift parity between the vbt and iterative engines is bit-identical
     BY CONSTRUCTION (both call translator._eval_condition).

  4. TestEndToEnd — a spec with RSICondition gating entry/exit runs
     through both vbt (`run_backtest`) and iterative
     (`run_iterative_backtest`) paths; ±2× trade-count envelope.

Runs in the default suite; total wall-clock < 1 s.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import (
    RSICondition,
    StrategySpec,
    Timeframe,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


# ---- Schema validation ----------------------------------------------------


class TestRSIConditionSchema:
    def test_basic_construction(self) -> None:
        c = RSICondition(threshold=30, comparison="below")
        assert c.type == "rsi"
        assert c.period == 14  # default
        assert c.threshold == 30
        assert c.comparison == "below"
        assert c.source == "close"  # default

    def test_explicit_fields(self) -> None:
        c = RSICondition(period=7, threshold=70.5, comparison="above", source="high")
        assert c.period == 7
        assert c.threshold == 70.5
        assert c.comparison == "above"
        assert c.source == "high"

    @pytest.mark.parametrize("bad_period", [1, 0, -1, 101, 200])
    def test_invalid_period_rejected(self, bad_period: int) -> None:
        # period bounded ge=2 le=100.
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            RSICondition(period=bad_period, threshold=30, comparison="below")

    @pytest.mark.parametrize("bad_threshold", [-0.1, -1, 100.1, 101, 1000])
    def test_invalid_threshold_rejected(self, bad_threshold: float) -> None:
        # threshold bounded ge=0 le=100 (RSI's natural range).
        with pytest.raises(Exception):  # noqa: B017
            RSICondition(threshold=bad_threshold, comparison="below")

    def test_threshold_bounds_inclusive(self) -> None:
        # 0 and 100 are valid (inclusive bounds).
        assert RSICondition(threshold=0, comparison="below").threshold == 0
        assert RSICondition(threshold=100, comparison="above").threshold == 100

    def test_invalid_comparison_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            RSICondition(threshold=30, comparison="lt")  # type: ignore[arg-type]

    def test_invalid_source_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            RSICondition(threshold=30, comparison="below", source="volume")  # type: ignore[arg-type]

    def test_round_trip_preserves_equality(self) -> None:
        c = RSICondition(period=21, threshold=68.0, comparison="crosses_below", source="low")
        roundtripped = RSICondition.model_validate_json(c.model_dump_json())
        assert roundtripped == c

    def test_routes_via_condition_discriminator(self) -> None:
        from marketmind_shared.schemas.strategy_spec import Condition
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Condition)
        parsed = adapter.validate_python(
            {"type": "rsi", "threshold": 30, "comparison": "below"},
        )
        assert isinstance(parsed, RSICondition)
        assert parsed.threshold == 30
        assert parsed.comparison == "below"


# ---- _eval_rsi helper unit tests ------------------------------------------


def _v_shape_df() -> pd.DataFrame:
    """60-bar 1H UTC fixture: 10 flat warmup bars, a 20-bar decline,
    then a 30-bar recovery. Drives RSI down to 0 in the decline and
    back up through 30 in the recovery.

    HAND-VERIFIED RSI behaviour (period=14, source=close), confirmed by
    printing the full series during test development:

      - RSI is NaN on bars 0..12 (warmup), 0.000 on bars 13..29 (the
        whole decline has no up-moves so the gain-EMA is 0).
      - During the recovery RSI climbs:
            bar 30 -> 11.065   bar 31 -> 20.534   bar 32 -> 28.708
            bar 33 -> 35.818   bar 34 -> 42.043   ...
      - RSI < 30 ("below") is TRUE on bars 13..32 inclusive = 20 bars.
      - RSI > 30 ("above") is TRUE on bars 33..59 inclusive = 27 bars.
      - "crosses_above" 30 fires on EXACTLY bar 33: bar 32's RSI is
        28.708 (<= 30) and bar 33's is 35.818 (> 30). No other bar
        transitions up through 30.
      - "crosses_below" 30 never fires (RSI is already 0 before bar 13;
        it never descends through 30 from above in this fixture).
    """
    closes: list[float] = [100.0] * 10
    v = 100.0
    for _ in range(20):
        v -= 2.0
        closes.append(v)
    for _ in range(30):
        v += 2.5
        closes.append(v)
    closes = closes[:60]
    arr = np.array(closes)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=60, freq="1h")
    return pd.DataFrame(
        {
            "open": arr,
            "high": arr + 0.5,
            "low": arr - 0.5,
            "close": arr,
            "volume": [1e6] * 60,
        },
        index=idx,
    )


def _inverted_v_df() -> pd.DataFrame:
    """60-bar 1H UTC fixture: 10 flat warmup bars, a 20-bar rise into
    overbought, then a 30-bar decline through RSI 30.

    HAND-VERIFIED RSI behaviour (period=14, source=close):
      - During the decline RSI falls:
            bar 41 -> 30.135   bar 42 -> 27.616   bar 43 -> 25.335 ...
      - "crosses_below" 30 fires on EXACTLY bar 42: bar 41's RSI is
        30.135 (>= 30) and bar 42's is 27.616 (< 30). No other bar
        transitions down through 30.
    """
    closes: list[float] = [100.0] * 10
    v = 100.0
    for _ in range(20):
        v += 2.0
        closes.append(v)
    for _ in range(30):
        v -= 2.5
        closes.append(v)
    closes = closes[:60]
    arr = np.array(closes)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=60, freq="1h")
    return pd.DataFrame(
        {
            "open": arr,
            "high": arr + 0.5,
            "low": arr - 0.5,
            "close": arr,
            "volume": [1e6] * 60,
        },
        index=idx,
    )


class TestEvalRSI:
    """Mask-correctness against HAND-VERIFIED literals (non-circular:
    the asserted bar indices and counts were read off a printed RSI
    series, not produced by the helper under test)."""

    @staticmethod
    def _eval(cond: RSICondition, df: pd.DataFrame) -> pd.Series:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _eval_rsi,
        )

        return _eval_rsi(cond, df)

    def test_below_threshold_count_and_bounds(self) -> None:
        # RSI < 30 is TRUE on bars 13..32 inclusive (hand-verified) = 20.
        cond = RSICondition(threshold=30, comparison="below")
        mask = self._eval(cond, _v_shape_df())
        assert mask.sum() == 20
        true_idx = [i for i in range(60) if bool(mask.iloc[i])]
        assert true_idx == list(range(13, 33))  # 13..32 inclusive
        # Warmup NaN bars never fire.
        assert not bool(mask.iloc[0])
        assert not bool(mask.iloc[12])

    def test_above_threshold_count_and_bounds(self) -> None:
        # RSI > 30 is TRUE on bars 33..59 inclusive (hand-verified) = 27.
        cond = RSICondition(threshold=30, comparison="above")
        mask = self._eval(cond, _v_shape_df())
        assert mask.sum() == 27
        true_idx = [i for i in range(60) if bool(mask.iloc[i])]
        assert true_idx == list(range(33, 60))  # 33..59 inclusive

    def test_crosses_above_fires_on_exact_bar(self) -> None:
        # Hand-verified: bar 32 RSI=28.708 (<=30), bar 33 RSI=35.818
        # (>30). Cross-up fires on bar 33 and nowhere else.
        cond = RSICondition(threshold=30, comparison="crosses_above")
        mask = self._eval(cond, _v_shape_df())
        true_idx = [i for i in range(60) if bool(mask.iloc[i])]
        assert true_idx == [33]

    def test_crosses_below_never_fires_on_v_shape(self) -> None:
        # The V-shape decline drives RSI straight to 0; it is already
        # below 30 from bar 13 and never descends THROUGH 30 from above.
        cond = RSICondition(threshold=30, comparison="crosses_below")
        mask = self._eval(cond, _v_shape_df())
        assert mask.sum() == 0

    def test_crosses_below_fires_on_exact_bar(self) -> None:
        # Inverted-V fixture. Hand-verified: bar 41 RSI=30.135 (>=30),
        # bar 42 RSI=27.616 (<30). Cross-down fires on bar 42 only.
        cond = RSICondition(threshold=30, comparison="crosses_below")
        mask = self._eval(cond, _inverted_v_df())
        true_idx = [i for i in range(60) if bool(mask.iloc[i])]
        assert true_idx == [42]

    def test_mask_index_matches_input(self) -> None:
        df = _v_shape_df()
        cond = RSICondition(threshold=70, comparison="above")
        mask = self._eval(cond, df)
        assert mask.index.equals(df.index)
        assert mask.dtype == bool


# ---- Dispatcher identity --------------------------------------------------


class TestDispatcherIdentity:
    """The condition dispatcher (_eval_condition_on_tf) routes an
    RSICondition to the same _eval_rsi helper that's importable
    directly. assert_series_equal proves the engine has no parallel
    implementation of the RSI math — so vbt and iterative compute the
    exact same Series (drift parity bit-identical by construction)."""

    def test_dispatcher_output_matches_helper_call(self) -> None:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _Context,
            _eval_condition_on_tf,
            _eval_rsi,
        )

        data = _v_shape_df()
        ctx = _Context(spec=None, data={Timeframe.H1: data}, primary_index=data.index)  # type: ignore[arg-type]
        cond = RSICondition(threshold=30, comparison="crosses_above")

        via_dispatcher = _eval_condition_on_tf(cond, ctx, timeframe=Timeframe.H1)
        via_helper = _eval_rsi(cond, data)

        pd.testing.assert_series_equal(via_dispatcher, via_helper)


# ---- End-to-end vbt vs iterative envelope ---------------------------------


def _synthetic_ohlcv(n: int = 400) -> pd.DataFrame:
    """Oscillating series with enough swing to push RSI repeatedly
    below 30 and above 70 — UTC 1H aligned. Sinusoid + mild trend +
    noise produces multiple oversold dips → multiple RSI-gated trades.
    """
    rng = np.random.default_rng(11)
    base = 100 + 0.02 * np.arange(n) + 12 * np.sin(np.linspace(0, 20 * np.pi, n))
    noise = rng.normal(0, 0.8, n)
    closes = np.maximum(base + noise, 1.0)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )


def _spec_rsi_mean_reversion() -> StrategySpec:
    """Classic oscillator mean-reversion: enter long when RSI < 30
    (oversold), exit when RSI > 70 (overbought) or a 5% stop. Both
    legs are first-class RSICondition nodes.
    """
    spec_dict: dict[str, Any] = {
        "schema_version": "2.0",
        "name": "RSI mean reversion",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "rsi",
                "period": 14,
                "threshold": 30,
                "comparison": "below",
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}},
                {
                    "type": "condition",
                    "condition": {
                        "type": "rsi",
                        "period": 14,
                        "threshold": 70,
                        "comparison": "above",
                    },
                },
            ],
        },
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
    }
    spec, _warnings = validate_spec(spec_dict)
    return spec


class TestEndToEnd:
    def test_vbt_and_iterative_both_run_envelope(self) -> None:
        """Both engines produce trades within a ±2× envelope. Shared
        dispatcher means RSI evaluation is bit-identical; any residual
        trade-ledger difference is exit-tie-break, not RSI."""
        spec = _spec_rsi_mean_reversion()
        data = _synthetic_ohlcv(400)
        vbt_run = run_backtest(spec, _START, _END, 10_000.0, data_override={Timeframe.H1: data})
        it_run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert len(vbt_run.trades) > 0
        assert len(it_run.trades) > 0
        ratio = len(vbt_run.trades) / len(it_run.trades)
        assert 0.5 <= ratio <= 2.0, (
            f"vbt={len(vbt_run.trades)} iterative={len(it_run.trades)} "
            f"ratio={ratio:.2f} — too wide for shared-dispatcher case"
        )

    def test_empirical_entry_bars_are_oversold(self) -> None:
        """Every iterative trade's entry bar has RSI < 30 — the gate is
        actually gating, not just parsing.

        NON-CIRCULAR: the threshold 30 is the spec's literal, and RSI is
        recomputed here via the public ind.rsi (the same Wilder function
        the engine uses) purely to read the entry-bar value. We assert
        the entry-bar RSI is below the spec's stated oversold level — a
        property of the strategy, hand-reasoned, not the engine's own
        boolean output.

        Empirically (verified during test development): the iterative
        engine reports entry_time as the SIGNAL bar's open time, so the
        RSI looked up at entry_time is the RSI that fired the gate.
        """
        from marketmind_workers.backtest import indicators as ind

        spec = _spec_rsi_mean_reversion()
        data = _synthetic_ohlcv(400)
        run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert len(run.trades) > 0
        rsi = ind.rsi(data, 14, "close")
        for trade in run.trades:
            rsi_at_entry = float(rsi.loc[trade.entry_time])
            assert rsi_at_entry < 30, (
                f"trade entered at RSI={rsi_at_entry:.2f} — not below the "
                f"30 oversold threshold the entry gate is supposed to enforce"
            )
