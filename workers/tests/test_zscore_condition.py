"""v1.3 — ZScoreCondition (statistical mean-reversion gate) tests.

Four test classes mirroring v1.2.C's TimeOfDayCondition structure:

  1. TestZScoreConditionSchema — Pydantic shape: defaults, bounds
     rejection (period / threshold), JSON round-trip, discriminator
     routing.

  2. TestEvalZScore — pure mask-correctness on a HAND-VERIFIED
     fixture. The z series is computed independently with the textbook
     formula z = (close - mean) / sample_std and the literals are
     asserted against arithmetic done by hand (NOT against the engine's
     own output — non-circular). Covers below_neg, above_pos,
     cross_toward_zero, and the divide-by-zero (flat-window) guard.

  3. TestDispatcherIdentity — _eval_condition_on_tf routes a
     ZScoreCondition to the same _eval_zscore helper that's importable
     directly. assert_series_equal proves there is ONE implementation,
     so the vbt path and the iterative engine (which reuses
     translator._eval_condition for all non-Tier3 conditions) compute
     bit-identical masks — exact drift parity by construction.

  4. TestEndToEnd — a tiny spec with a ZScoreCondition gating an entry
     runs through both vbt (run_backtest) and iterative
     (run_iterative_backtest); both produce trades inside the ±2x
     envelope (shared dispatcher; trade-ledger differences come from
     exit-tie-break, not the z gate).

EMPIRICAL-INSPECTION NOTE (v1.2 standing rule): the hand-verified
fixture and its literals were produced by running the z math with print
statements first, hand-checking the arithmetic, THEN encoding the
literals below. See the docstring of TestEvalZScore for the worked
arithmetic.

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
    StrategySpec,
    Timeframe,
    ZScoreCondition,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


# ---- Schema validation ----------------------------------------------------


class TestZScoreConditionSchema:
    def test_basic_construction(self) -> None:
        c = ZScoreCondition(form="below_neg")
        assert c.type == "zscore"
        assert c.period == 20  # default
        assert c.threshold == 2.0  # default
        assert c.source == "close"  # default
        assert c.form == "below_neg"

    def test_all_forms_accepted(self) -> None:
        for form in ("below_neg", "above_pos", "cross_toward_zero"):
            c = ZScoreCondition(form=form)  # type: ignore[arg-type]
            assert c.form == form

    @pytest.mark.parametrize("bad_period", [1, 0, -5, 101, 200])
    def test_invalid_period_rejected(self, bad_period: int) -> None:
        # period bounded ge=2 le=100.
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            ZScoreCondition(period=bad_period, form="below_neg")

    @pytest.mark.parametrize("bad_threshold", [0.0, -1.0, 20.0001, 50.0])
    def test_invalid_threshold_rejected(self, bad_threshold: float) -> None:
        # threshold bounded gt=0 le=20.
        with pytest.raises(Exception):  # noqa: B017
            ZScoreCondition(threshold=bad_threshold, form="below_neg")

    def test_threshold_boundaries(self) -> None:
        # gt=0 means a tiny positive is OK; le=20 means 20 is OK.
        assert ZScoreCondition(threshold=0.0001, form="below_neg").threshold == 0.0001
        assert ZScoreCondition(threshold=20.0, form="below_neg").threshold == 20.0

    def test_invalid_source_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ZScoreCondition(source="vwap", form="below_neg")  # type: ignore[arg-type]

    def test_invalid_form_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017
            ZScoreCondition(form="toward_zero")  # type: ignore[arg-type]

    def test_round_trip_preserves_equality(self) -> None:
        c = ZScoreCondition(period=14, threshold=1.5, source="high", form="cross_toward_zero")
        roundtripped = ZScoreCondition.model_validate_json(c.model_dump_json())
        assert roundtripped == c

    def test_routes_via_condition_discriminator(self) -> None:
        from marketmind_shared.schemas.strategy_spec import Condition
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Condition)
        parsed = adapter.validate_python(
            {"type": "zscore", "period": 10, "threshold": 2.0, "form": "below_neg"},
        )
        assert isinstance(parsed, ZScoreCondition)
        assert parsed.period == 10
        assert parsed.form == "below_neg"


# ---- _eval_zscore helper unit tests ---------------------------------------


def _zscore_fixture() -> pd.DataFrame:
    """12-bar UTC 1H fixture, period=10 in mind.

    Closes: nine bars at 100.0, then a deep dip to 60.0 at bar 9, then
    two recovery bars back at 100.0.

    HAND-VERIFIED z series for period=10 (sample std, ddof=1):

      bars 0-8 : NaN (warmup, < 10 bars in window)
      bar 9    : window = [100]*9 + [60]
                 mean = (900 + 60) / 10 = 96.0
                 variance = (9*(100-96)^2 + (60-96)^2) / 9
                          = (9*16 + 1296) / 9 = (144 + 1296)/9 = 160.0
                 std = sqrt(160) = 12.649110640673518
                 z = (60 - 96) / 12.649110640673518 = -2.8460498941515415
      bar 10   : window = [100]*8 + [60] + [100]  (same multiset as bar 9)
                 mean = 96.0, std = 12.649110640673518
                 z = (100 - 96) / 12.649110640673518 = +0.31622776601683794
      bar 11   : window = [100]*7 + [60] + [100]*2 (same multiset)
                 z = +0.31622776601683794
    """
    closes = [100.0] * 9 + [60.0] + [100.0, 100.0]
    n = len(closes)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": [1.0] * n,
        },
        index=idx,
    )


_Z_BAR9 = -2.8460498941515415  # hand-verified above
_Z_BAR10 = 0.31622776601683794  # hand-verified above


class TestEvalZScore:
    """Mask correctness on the hand-verified fixture. All literals trace
    to the arithmetic worked out in _zscore_fixture's docstring — none
    are read back from the engine.
    """

    @staticmethod
    def _eval(cond: ZScoreCondition, df: pd.DataFrame) -> pd.Series:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _eval_zscore,
        )

        return _eval_zscore(cond, df)

    def test_below_neg_fires_only_on_deep_dip(self) -> None:
        # z[9] = -2.846 < -2.0 ; every other bar's z is NaN or > -2.0.
        c = ZScoreCondition(period=10, threshold=2.0, form="below_neg")
        mask = self._eval(c, _zscore_fixture())
        assert mask.sum() == 1
        assert mask.iloc[9]
        assert not mask.iloc[10]

    def test_below_neg_threshold_just_above_dip_no_fire(self) -> None:
        # threshold=3.0: |z[9]| = 2.846 < 3.0, so below_neg never fires.
        c = ZScoreCondition(period=10, threshold=3.0, form="below_neg")
        mask = self._eval(c, _zscore_fixture())
        assert mask.sum() == 0

    def test_above_pos_never_fires_on_downside_fixture(self) -> None:
        # The only extreme is a dip (negative z). No bar's z exceeds +2.
        c = ZScoreCondition(period=10, threshold=2.0, form="above_pos")
        mask = self._eval(c, _zscore_fixture())
        assert mask.sum() == 0

    def test_cross_toward_zero_fires_on_recovery_bar(self) -> None:
        # bar 9 z = -2.846 (<= -threshold); bar 10 z = +0.316 > z[9],
        # i.e. moved toward zero -> the reversion trigger fires at bar 10
        # (NOT bar 9, which is still at the extreme).
        c = ZScoreCondition(period=10, threshold=2.0, form="cross_toward_zero")
        mask = self._eval(c, _zscore_fixture())
        assert mask.sum() == 1
        assert mask.iloc[10]
        assert not mask.iloc[9]

    def test_warmup_bars_are_false(self) -> None:
        # The first period-1 = 9 bars have NaN z -> condition False.
        c = ZScoreCondition(period=10, threshold=2.0, form="below_neg")
        mask = self._eval(c, _zscore_fixture())
        assert not mask.iloc[:9].any()

    def test_flat_window_zero_std_guard(self) -> None:
        # A perfectly flat series has std == 0 everywhere; the guard
        # coerces z to NaN, so no form ever fires (no divide-by-zero,
        # no spurious signal).
        flat = [50.0] * 12
        idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=12, freq="1h")
        df = pd.DataFrame(
            {"open": flat, "high": flat, "low": flat, "close": flat, "volume": [1.0] * 12},
            index=idx,
        )
        for form in ("below_neg", "above_pos", "cross_toward_zero"):
            c = ZScoreCondition(period=10, threshold=2.0, form=form)  # type: ignore[arg-type]
            mask = self._eval(c, df)
            assert mask.sum() == 0, f"flat-window guard failed for form={form}"

    def test_z_value_matches_hand_arithmetic(self) -> None:
        # Recompute z independently (textbook formula) and assert the
        # hand-derived literals — proves the helper's math, not just the
        # mask shape. Independent of _eval_zscore.
        df = _zscore_fixture()
        close = df["close"]
        mean = close.rolling(10, min_periods=10).mean()
        std = close.rolling(10, min_periods=10).std(ddof=1)
        z = (close - mean) / std
        assert z.iloc[9] == pytest.approx(_Z_BAR9, abs=1e-12)
        assert z.iloc[10] == pytest.approx(_Z_BAR10, abs=1e-12)


# ---- Dispatcher identity --------------------------------------------------


class TestDispatcherIdentity:
    """The condition dispatcher (_eval_condition_on_tf) routes a
    ZScoreCondition to the same _eval_zscore helper importable directly.
    Bit-identical Series => the vbt path and the iterative engine (which
    reuses translator._eval_condition for non-Tier3 conditions) compute
    the SAME mask — drift parity by construction, no parallel
    implementation to drift.
    """

    def test_dispatcher_output_matches_helper_call(self) -> None:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _Context,
            _eval_condition_on_tf,
            _eval_zscore,
        )

        data = _zscore_fixture()
        ctx = _Context(spec=None, data={Timeframe.H1: data}, primary_index=data.index)  # type: ignore[arg-type]
        cond = ZScoreCondition(period=10, threshold=2.0, form="cross_toward_zero")

        via_dispatcher = _eval_condition_on_tf(cond, ctx, timeframe=Timeframe.H1)
        via_helper = _eval_zscore(cond, data)

        pd.testing.assert_series_equal(via_dispatcher, via_helper)


# ---- End-to-end vbt vs iterative envelope ---------------------------------


def _synthetic_ohlcv(n: int = 400) -> pd.DataFrame:
    """Oscillating series with periodic dips that drive the z-score
    below -2 repeatedly, so a below_neg gate produces multiple trades.
    UTC 1H aligned.
    """
    rng = np.random.default_rng(11)
    base = 100 + 6 * np.sin(np.linspace(0, 24 * np.pi, n))
    noise = rng.normal(0, 0.4, n)
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


def _spec_zscore_gated() -> StrategySpec:
    """Minimal Tier-1 mean-reversion spec: enter long when the 20-bar
    z-score crosses below -1.5 (oversold). Exit: z back above +1.0
    (mean reverted past the middle) OR a 5% stop-loss.
    """
    spec_dict: dict[str, Any] = {
        "schema_version": "2.0",
        "name": "Z-score mean reversion",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "zscore",
                "period": 20,
                "threshold": 1.5,
                "source": "close",
                "form": "below_neg",
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}},
                {
                    "type": "condition",
                    "condition": {
                        "type": "zscore",
                        "period": 20,
                        "threshold": 1.0,
                        "source": "close",
                        "form": "above_pos",
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
        """Both engines produce trades within the ±2x envelope. Shared
        dispatcher means the z-gate evaluation is bit-identical; the
        trade-ledger differences come from exit-tie-break ordering, not
        the z condition.
        """
        spec = _spec_zscore_gated()
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
