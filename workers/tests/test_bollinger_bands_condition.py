"""v1.3 — BollingerBandsCondition (volatility-band mean-reversion +
squeeze breakout) tests.

Four test classes mirroring v1.2.A / v1.2.C structure:

  1. TestBollingerBandsConditionSchema — Pydantic shape: defaults,
     bounds rejection, JSON round-trip, the squeeze-param cross-field
     validator (required iff form=='squeeze'), discriminator routing.

  2. TestEvalBollingerBands — pure mask-correctness on hand-built
     fixtures for all three forms, with HAND-VERIFIED literals (see the
     empirical-inspection note in each test).

  3. TestDispatcherIdentity — _eval_condition_on_tf routes a
     BollingerBandsCondition to the same _eval_bollinger_bands helper
     that's importable directly. pd.testing.assert_series_equal proves
     a single implementation => exact drift parity between the vbt and
     iterative engines (the iterative engine reuses translator.
     _eval_condition for every non-Tier3 condition).

  4. TestEndToEnd — a mean-reversion spec gated by a below_lower
     BollingerBandsCondition runs through both vbt (`run_backtest`) and
     iterative (`run_iterative_backtest`); same ±2× envelope as the
     other stateless-condition primitives.

Runs in the default suite; total wall-clock < 2 s.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import (
    BollingerBandsCondition,
    Condition,
    StrategySpec,
    Timeframe,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest
from pydantic import TypeAdapter, ValidationError

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


# ---- Schema validation ----------------------------------------------------


class TestBollingerBandsConditionSchema:
    def test_basic_construction_defaults(self) -> None:
        c = BollingerBandsCondition(form="below_lower")
        assert c.type == "bollinger_bands"
        assert c.period == 20
        assert c.num_std == 2.0
        assert c.source == "close"
        assert c.form == "below_lower"
        assert c.squeeze_window is None
        assert c.squeeze_percentile is None

    def test_above_upper_construction(self) -> None:
        c = BollingerBandsCondition(form="above_upper", period=10, num_std=2.5)
        assert c.form == "above_upper"
        assert c.period == 10
        assert c.num_std == 2.5

    def test_squeeze_construction(self) -> None:
        c = BollingerBandsCondition(
            form="squeeze",
            squeeze_window=50,
            squeeze_percentile=0.1,
        )
        assert c.form == "squeeze"
        assert c.squeeze_window == 50
        assert c.squeeze_percentile == 0.1

    @pytest.mark.parametrize("bad_period", [1, 0, -5, 101, 500])
    def test_period_bounds_rejected(self, bad_period: int) -> None:
        with pytest.raises(ValidationError):
            BollingerBandsCondition(form="below_lower", period=bad_period)

    @pytest.mark.parametrize("bad_std", [0.0, -1.0, 5.0001, 10.0])
    def test_num_std_bounds_rejected(self, bad_std: float) -> None:
        # gt=0, le=5
        with pytest.raises(ValidationError):
            BollingerBandsCondition(form="below_lower", num_std=bad_std)

    def test_num_std_upper_bound_inclusive(self) -> None:
        c = BollingerBandsCondition(form="below_lower", num_std=5.0)
        assert c.num_std == 5.0

    @pytest.mark.parametrize("bad_window", [1, 0, -1, 10_001, 50_000])
    def test_squeeze_window_bounds_rejected(self, bad_window: int) -> None:
        with pytest.raises(ValidationError):
            BollingerBandsCondition(
                form="squeeze",
                squeeze_window=bad_window,
                squeeze_percentile=0.1,
            )

    @pytest.mark.parametrize("bad_pct", [-0.1, 1.0001, 2.0, -5.0])
    def test_squeeze_percentile_bounds_rejected(self, bad_pct: float) -> None:
        with pytest.raises(ValidationError):
            BollingerBandsCondition(
                form="squeeze",
                squeeze_window=50,
                squeeze_percentile=bad_pct,
            )

    def test_squeeze_requires_both_params(self) -> None:
        # form='squeeze' but missing squeeze_window -> rejected.
        with pytest.raises(ValidationError, match="squeeze_params_missing"):
            BollingerBandsCondition(form="squeeze", squeeze_percentile=0.1)
        # missing squeeze_percentile -> rejected.
        with pytest.raises(ValidationError, match="squeeze_params_missing"):
            BollingerBandsCondition(form="squeeze", squeeze_window=50)
        # both missing -> rejected.
        with pytest.raises(ValidationError, match="squeeze_params_missing"):
            BollingerBandsCondition(form="squeeze")

    def test_non_squeeze_forbids_squeeze_params(self) -> None:
        # below_lower with a dangling squeeze param -> rejected.
        with pytest.raises(ValidationError, match="squeeze_params_forbidden"):
            BollingerBandsCondition(form="below_lower", squeeze_window=50)
        with pytest.raises(ValidationError, match="squeeze_params_forbidden"):
            BollingerBandsCondition(form="above_upper", squeeze_percentile=0.2)

    def test_round_trip_preserves_equality_below_lower(self) -> None:
        c = BollingerBandsCondition(form="below_lower", period=15, num_std=1.5)
        roundtripped = BollingerBandsCondition.model_validate_json(c.model_dump_json())
        assert roundtripped == c

    def test_round_trip_preserves_equality_squeeze(self) -> None:
        c = BollingerBandsCondition(
            form="squeeze",
            period=20,
            num_std=2.0,
            squeeze_window=120,
            squeeze_percentile=0.05,
        )
        roundtripped = BollingerBandsCondition.model_validate_json(c.model_dump_json())
        assert roundtripped == c

    def test_routes_via_condition_discriminator(self) -> None:
        adapter = TypeAdapter(Condition)
        parsed = adapter.validate_python(
            {
                "type": "bollinger_bands",
                "form": "squeeze",
                "squeeze_window": 50,
                "squeeze_percentile": 0.1,
            },
        )
        assert isinstance(parsed, BollingerBandsCondition)
        assert parsed.form == "squeeze"
        assert parsed.squeeze_window == 50


# ---- _eval_bollinger_bands helper unit tests ------------------------------


def _flat_with_spikes_df() -> pd.DataFrame:
    """24-bar 1H UTC fixture: 20 flat bars at 100.0, then a sharp drop to
    95.0 (index 20), back to 100, a sharp spike to 106.0 (index 22), back
    to 100. Designed so a period=10 / num_std=2 band is exactly 100 while
    flat, then widens only around the spikes.
    """
    n = 24
    close = np.full(n, 100.0)
    close[20] = 95.0
    close[22] = 106.0
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": [1e6] * n,
        },
        index=idx,
    )


def _squeeze_df() -> pd.DataFrame:
    """40-bar 1H UTC fixture with a deliberate volatility regime:
    a widening sawtooth (0..14), a near-flat low-volatility coil
    (15..29), then a re-expansion (30..39). With period=5 / num_std=2 and
    squeeze_window=10, the bandwidth percentile collapses into the low
    tail across the coil — a textbook squeeze.
    """
    n = 40
    close = np.empty(n)
    close[0:15] = 100 + np.array(
        [0, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8, -8],
        dtype=float,
    )
    close[15:30] = 100 + np.array(
        [0.0, 0.1, -0.1, 0.05, -0.05, 0.1, -0.1, 0.0, 0.05, -0.05, 0.1, -0.1, 0.0, 0.05, -0.05],
    )
    close[30:40] = 100 + np.array(
        [0, 3, -3, 6, -6, 9, -9, 12, -12, 15],
        dtype=float,
    )
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": [1e6] * n,
        },
        index=idx,
    )


def _eval(cond: BollingerBandsCondition, df: pd.DataFrame) -> pd.Series:
    from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
        _eval_bollinger_bands,
    )

    return _eval_bollinger_bands(cond, df)


class TestEvalBollingerBands:
    """Pure mask-correctness with HAND-VERIFIED literals.

    Empirical inspection (run during development, period=10/num_std=2 on
    _flat_with_spikes_df): the band sits at exactly 100 while close is
    flat, so neither below_lower nor above_upper fires there. At index 20
    close=95.0 < lower=96.500 -> below_lower fires (and ONLY index 20).
    At index 22 close=106.0 > upper=105.036 -> above_upper fires (and ONLY
    index 22). Verified by printing close/lower/upper per bar.
    """

    def test_below_lower_fires_only_at_the_drop(self) -> None:
        cond = BollingerBandsCondition(form="below_lower", period=10, num_std=2.0)
        mask = _eval(cond, _flat_with_spikes_df())
        true_idx = [int(i) for i in np.where(mask.to_numpy())[0]]
        assert true_idx == [20]

    def test_above_upper_fires_only_at_the_spike(self) -> None:
        cond = BollingerBandsCondition(form="above_upper", period=10, num_std=2.0)
        mask = _eval(cond, _flat_with_spikes_df())
        true_idx = [int(i) for i in np.where(mask.to_numpy())[0]]
        assert true_idx == [22]

    def test_below_lower_no_false_positive_when_inside_bands(self) -> None:
        # A perfectly flat series never breaks its own (zero-width) band:
        # close == lower everywhere, and below_lower is STRICT (<), so the
        # mask is all-False post-warmup.
        n = 30
        close = np.full(n, 100.0)
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {"open": close, "high": close, "low": close, "close": close, "volume": [1e6] * n},
            index=idx,
        )
        cond = BollingerBandsCondition(form="below_lower", period=10, num_std=2.0)
        mask = _eval(cond, df)
        assert mask.sum() == 0

    def test_squeeze_fires_across_the_coil(self) -> None:
        """Empirical inspection (period=5, num_std=2, squeeze_window=10 on
        _squeeze_df): the rolling bandwidth percentile is <= 0.2 at bar
        indices [18, 19, 20, 21, 22, 23, 30] — the low-volatility coil
        plus the first re-expansion bar (whose 10-bar trailing window
        still mostly covers the coil). Hand-verified by printing the
        bandwidth and its rolling percentile per bar.
        """
        cond = BollingerBandsCondition(
            form="squeeze",
            period=5,
            num_std=2.0,
            squeeze_window=10,
            squeeze_percentile=0.2,
        )
        mask = _eval(cond, _squeeze_df())
        true_idx = [int(i) for i in np.where(mask.to_numpy())[0]]
        assert true_idx == [18, 19, 20, 21, 22, 23, 30]

    def test_squeeze_warmup_is_false(self) -> None:
        # The first (period - 1) + (squeeze_window - 1) bars cannot have a
        # valid rolling percentile; the mask is False there (NaN <= x is
        # False). On _squeeze_df with period=5, squeeze_window=10, the
        # earliest possible True is bar index 13 (bandwidth first defined
        # at index 4, percentile needs 10 of them -> index 13). Indices
        # 0..12 must all be False.
        cond = BollingerBandsCondition(
            form="squeeze",
            period=5,
            num_std=2.0,
            squeeze_window=10,
            squeeze_percentile=0.2,
        )
        mask = _eval(cond, _squeeze_df())
        assert not mask.iloc[0:13].any()


# ---- Dispatcher identity --------------------------------------------------


class TestDispatcherIdentity:
    """The condition dispatcher (_eval_condition_on_tf) routes a
    BollingerBandsCondition to the same _eval_bollinger_bands helper
    that's importable directly. Because the iterative engine reaches this
    dispatcher via translator._eval_condition for every non-Tier3
    condition, a bit-identical Series here is the exact drift-parity
    guarantee — the engines cannot diverge on a stateless condition that
    has only one implementation.
    """

    @pytest.mark.parametrize(
        "cond",
        [
            BollingerBandsCondition(form="below_lower", period=10, num_std=2.0),
            BollingerBandsCondition(form="above_upper", period=10, num_std=2.0),
            BollingerBandsCondition(
                form="squeeze",
                period=5,
                num_std=2.0,
                squeeze_window=10,
                squeeze_percentile=0.2,
            ),
        ],
    )
    def test_dispatcher_output_matches_helper_call(
        self,
        cond: BollingerBandsCondition,
    ) -> None:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _Context,
            _eval_bollinger_bands,
            _eval_condition_on_tf,
        )

        data = _squeeze_df()
        ctx = _Context(spec=None, data={Timeframe.H1: data}, primary_index=data.index)  # type: ignore[arg-type]

        via_dispatcher = _eval_condition_on_tf(cond, ctx, timeframe=Timeframe.H1)
        via_helper = _eval_bollinger_bands(cond, data)

        pd.testing.assert_series_equal(via_dispatcher, via_helper)


# ---- End-to-end vbt vs iterative envelope ---------------------------------


def _oscillating_ohlcv(n: int = 300) -> pd.DataFrame:
    """Sinusoidal mean-reverting series with noise — UTC 1H aligned. The
    repeated dips below the lower Bollinger band drive multiple
    below_lower entries; the take-profit / stop-loss exits close them.
    """
    rng = np.random.default_rng(11)
    base = 100 + 5 * np.sin(np.linspace(0, 20 * np.pi, n))
    noise = rng.normal(0, 1.5, n)
    closes = np.maximum(base + noise, 1.0)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
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


def _spec_below_lower_mean_rev() -> StrategySpec:
    """Long mean-reversion: enter when close < lower Bollinger band
    (period 20, num_std 2), exit on a 3% take-profit or 5% stop-loss.
    """
    spec_dict: dict[str, Any] = {
        "schema_version": "2.0",
        "name": "BB mean-reversion",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "bollinger_bands",
                "period": 20,
                "num_std": 2.0,
                "form": "below_lower",
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}},
                {"type": "take_profit", "method": {"kind": "percent", "value": 0.03}},
            ],
        },
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
    }
    spec, _warnings = validate_spec(spec_dict)
    return spec


class TestEndToEnd:
    def test_vbt_and_iterative_both_run_envelope(self) -> None:
        """Both engines produce trades within a ±2× envelope.

        Empirical inspection (run during development on
        _oscillating_ohlcv(300) with seed=11): vbt produced 10 trades and
        the iterative engine 11 (ratio 0.91). The small gap is the known
        exit-tie-break difference between the engines — unrelated to the
        bollinger_bands gate, which is bit-identical (proved by
        TestDispatcherIdentity). We assert the loose envelope rather than
        the exact counts so the test is robust to engine-internal
        rounding.
        """
        spec = _spec_below_lower_mean_rev()
        data = _oscillating_ohlcv(300)
        vbt_run = run_backtest(
            spec,
            _START,
            _END,
            10_000.0,
            data_override={Timeframe.H1: data},
        )
        it_run = run_iterative_backtest(
            spec,
            {Timeframe.H1: data},
            _START,
            _END,
            10_000.0,
        )
        assert len(vbt_run.trades) > 0
        assert len(it_run.trades) > 0
        ratio = len(vbt_run.trades) / len(it_run.trades)
        assert 0.5 <= ratio <= 2.0, (
            f"vbt={len(vbt_run.trades)} iterative={len(it_run.trades)} "
            f"ratio={ratio:.2f} — too wide for shared-dispatcher case"
        )
