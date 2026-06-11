"""v1.2.C — TimeOfDayCondition (hour-of-day entry/exit gate) tests.

Four test classes mirroring v1.2.A / v1.2.B structure:

  1. TestTimeOfDayConditionSchema — Pydantic shape: bounds, valid/
     invalid hours, JSON round-trip, defaults.

  2. TestEvalTimeOfDay — pure unit tests on the `_eval_time_of_day`
     helper: standard windows, wrap-around, inclusive vs exclusive
     end, single-hour window, entire-day window.

  3. TestDispatcherIdentity — _eval_condition_on_tf routes a
     TimeOfDayCondition to the same helper that's importable
     directly. Mirrors v1.2.A's "dispatcher uses helper" check.

  4. TestEndToEnd — a tiny spec with TimeOfDayCondition gating an
     entry runs through both vbt (`run_backtest`) and iterative
     (`run_iterative_backtest`) paths. Same ±2× envelope for trade
     counts as v1.2.A — both engines share the dispatcher, but the
     vbt-vs-iterative trade ledger has exit-tie-break differences
     unrelated to time_of_day.

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
    TimeOfDayCondition,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


# ---- Schema validation ----------------------------------------------------


class TestTimeOfDayConditionSchema:
    def test_basic_construction(self) -> None:
        c = TimeOfDayCondition(start_hour_utc=9, end_hour_utc=17)
        assert c.type == "time_of_day"
        assert c.start_hour_utc == 9
        assert c.end_hour_utc == 17
        assert c.inclusive_end is True  # default

    def test_inclusive_end_default_true(self) -> None:
        # Matches how humans describe time windows ("9 to 17" usually
        # includes both endpoints).
        c = TimeOfDayCondition(start_hour_utc=0, end_hour_utc=23)
        assert c.inclusive_end is True

    def test_inclusive_end_override(self) -> None:
        c = TimeOfDayCondition(start_hour_utc=9, end_hour_utc=17, inclusive_end=False)
        assert c.inclusive_end is False

    @pytest.mark.parametrize("bad_hour", [-1, 24, 100, -100])
    def test_invalid_hours_rejected(self, bad_hour: int) -> None:
        # Both start and end are bounded ge=0 le=23.
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            TimeOfDayCondition(start_hour_utc=bad_hour, end_hour_utc=12)
        with pytest.raises(Exception):  # noqa: B017
            TimeOfDayCondition(start_hour_utc=12, end_hour_utc=bad_hour)

    def test_wrap_around_window_accepted(self) -> None:
        # start > end is the wrap-around case (spans midnight). Must
        # validate cleanly — the engine handles the boolean OR.
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=2)
        assert c.start_hour_utc == 22
        assert c.end_hour_utc == 2

    def test_round_trip_preserves_equality(self) -> None:
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=2, inclusive_end=False)
        roundtripped = TimeOfDayCondition.model_validate_json(c.model_dump_json())
        assert roundtripped == c

    def test_routes_via_condition_discriminator(self) -> None:
        # Confirms the discriminated union picks the right variant.
        from marketmind_shared.schemas.strategy_spec import Condition
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Condition)
        parsed = adapter.validate_python(
            {"type": "time_of_day", "start_hour_utc": 22, "end_hour_utc": 23},
        )
        assert isinstance(parsed, TimeOfDayCondition)
        assert parsed.start_hour_utc == 22


# ---- _eval_time_of_day helper unit tests ----------------------------------


def _hourly_df(days: int = 2) -> pd.DataFrame:
    """48-bar (2-day) UTC-aware 1H DataFrame, every column constant 1.0
    (the time_of_day evaluator only consults the index)."""
    n = 24 * days
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "volume": [1.0] * n,
        },
        index=idx,
    )


class TestEvalTimeOfDay:
    """Pure mask-correctness across edge cases. Two-day fixture so every
    test exercises the windowing across multiple days.
    """

    @staticmethod
    def _eval(cond: TimeOfDayCondition, df: pd.DataFrame) -> pd.Series:
        # Import via the translator module so the dispatcher's
        # internal helper is exercised directly.
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _eval_time_of_day,
        )

        return _eval_time_of_day(cond, df)

    def test_standard_window_inclusive(self) -> None:
        # 9-17 inclusive = hours 9,10,11,12,13,14,15,16,17 = 9 hours
        c = TimeOfDayCondition(start_hour_utc=9, end_hour_utc=17)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 18  # 9 hours x 2 days

    def test_standard_window_exclusive(self) -> None:
        # 9-17 exclusive = hours 9,10,11,12,13,14,15,16 = 8 hours
        c = TimeOfDayCondition(start_hour_utc=9, end_hour_utc=17, inclusive_end=False)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 16  # 8 hours x 2 days

    def test_wrap_around_inclusive(self) -> None:
        # 22-2 inclusive (wraps midnight) = hours 22,23,0,1,2 = 5 hours
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=2)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 10  # 5 hours x 2 days

    def test_wrap_around_exclusive(self) -> None:
        # 22-2 exclusive = hours 22,23,0,1 = 4 hours
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=2, inclusive_end=False)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 8

    def test_single_hour_window(self) -> None:
        # 22-22 inclusive = hour 22 only = 1 hour per day
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=22)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 2  # 1 hour x 2 days

    def test_single_hour_exclusive_is_empty(self) -> None:
        # 22-22 exclusive = empty window (hour >= 22 AND hour < 22) = no bars
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=22, inclusive_end=False)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 0

    def test_entire_day_window(self) -> None:
        # 0-23 inclusive = all 24 hours = every bar True
        c = TimeOfDayCondition(start_hour_utc=0, end_hour_utc=23)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 48  # every bar
        assert mask.all()

    def test_hunt_6b_intraday_seasonality(self) -> None:
        """Exact Hunt 6B case: hold long during the 22:00 UTC and 23:00
        UTC hourly bars. start=22, end=23, inclusive_end=True (default).
        """
        c = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=23)
        mask = self._eval(c, _hourly_df(days=2))
        assert mask.sum() == 4  # 22 + 23 across 2 days = 4 bars
        # Verify the exact bars: 22, 23, 46, 47 (hours 22, 23 each day)
        true_indices = mask[mask].index.tolist()  # type: ignore[index]
        true_hours = [t.hour for t in true_indices]
        assert true_hours == [22, 23, 22, 23]


# ---- Dispatcher identity --------------------------------------------------


class TestDispatcherIdentity:
    """The condition dispatcher (_eval_condition_on_tf) routes a
    TimeOfDayCondition to the same _eval_time_of_day helper that's
    importable directly. Mirrors v1.2.A's "dispatcher uses helper"
    pattern — proves the engine doesn't have a parallel implementation
    of the boolean math.
    """

    def test_dispatcher_output_matches_helper_call(self) -> None:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _Context,
            _eval_condition_on_tf,
            _eval_time_of_day,
        )

        data = _hourly_df(days=2)
        ctx = _Context(spec=None, data={Timeframe.H1: data}, primary_index=data.index)  # type: ignore[arg-type]
        cond = TimeOfDayCondition(start_hour_utc=22, end_hour_utc=23)

        via_dispatcher = _eval_condition_on_tf(cond, ctx, timeframe=Timeframe.H1)
        via_helper = _eval_time_of_day(cond, data)

        # Bit-identical Series.
        pd.testing.assert_series_equal(via_dispatcher, via_helper)


# ---- End-to-end vbt vs iterative envelope ---------------------------------


def _synthetic_ohlcv(n: int = 200) -> pd.DataFrame:
    """Trending series with enough variance to produce trades — UTC 1H
    aligned. The strategy uses a constant-True trigger gated by a
    time_of_day window, so the entry count is driven by the window.
    """
    rng = np.random.default_rng(7)
    base = 100 + 0.05 * np.arange(n) + 3 * np.sin(np.linspace(0, 8 * np.pi, n))
    noise = rng.normal(0, 0.5, n)
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


def _spec_time_of_day_gated() -> StrategySpec:
    """Minimal Tier-1 spec: enter long when close > previous bar's
    close AND time-of-day in 9-17 UTC. Exit: time-of-day NOT in
    9-17 (i.e. close-of-window) + stop-loss percent. The time_of_day
    in the entry condition produces a daily window of trading bars;
    in the exit condition it forces a close at end-of-window.
    """
    spec_dict: dict[str, Any] = {
        "schema_version": "2.0",
        "name": "Time-of-day gated trend",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "and",
                "conditions": [
                    {
                        "type": "compare",
                        "left": {"kind": "price", "field": "close"},
                        "op": ">",
                        "right": {
                            "kind": "lagged",
                            "expression": {"kind": "price", "field": "close"},
                            "bars_ago": 1,
                        },
                    },
                    {
                        "type": "time_of_day",
                        "start_hour_utc": 9,
                        "end_hour_utc": 17,
                    },
                ],
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}},
                {
                    "type": "condition",
                    "condition": {
                        "type": "not",
                        "condition": {
                            "type": "time_of_day",
                            "start_hour_utc": 9,
                            "end_hour_utc": 17,
                        },
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
        """Both engines produce trades within a sane envelope. Same
        rationale as v1.2.A's drift parity: shared dispatcher means
        entry-condition evaluation is bit-identical; trade-ledger
        differences come from exit-tie-break, not time_of_day.
        """
        spec = _spec_time_of_day_gated()
        data = _synthetic_ohlcv(200)
        vbt_run = run_backtest(spec, _START, _END, 10_000.0, data_override={Timeframe.H1: data})
        it_run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert len(vbt_run.trades) > 0
        assert len(it_run.trades) > 0
        ratio = len(vbt_run.trades) / len(it_run.trades)
        assert 0.5 <= ratio <= 2.0, (
            f"vbt={len(vbt_run.trades)} iterative={len(it_run.trades)} "
            f"ratio={ratio:.2f} — too wide for shared-dispatcher case"
        )

    def test_iterative_trades_only_enter_during_window(self) -> None:
        """Every trade's entry_time falls in the 9-17 UTC trigger
        window (inclusive). Proves the gate is actually gating, not
        just parsing.

        Empirically (verified during test development): the iterative
        engine reports entry_time as the SIGNAL BAR's open time, not
        the next-bar fill time. So a signal at hour 9 produces
        entry_time.hour == 9 (not 10). All entries must land in [9, 17]
        inclusive — anything at hour 0..8 or 18..23 means the gate
        failed.
        """
        spec = _spec_time_of_day_gated()
        data = _synthetic_ohlcv(200)
        run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert len(run.trades) > 0
        for trade in run.trades:
            entry_hour = trade.entry_time.hour
            assert 9 <= entry_hour <= 17, (
                f"trade entered at hour {entry_hour} — outside the 9-17 "
                f"trigger window the time_of_day gate is supposed to enforce"
            )

    def test_iterative_trades_exit_at_window_close(self) -> None:
        """The "NOT time_of_day" exit forces a close at the first bar
        outside the 9-17 window — i.e. hour 18. (Stop-loss could
        intervene earlier, but on this trending fixture no trade hits
        the 5% stop, so every exit lands at hour 18.) Confirms the
        exit-side of the gate works."""
        spec = _spec_time_of_day_gated()
        data = _synthetic_ohlcv(200)
        run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert len(run.trades) > 0
        exit_hours = {t.exit_time.hour for t in run.trades}
        # Most trades exit at hour 18 (first hour outside the window).
        # Allow some slack for stop-loss-driven exits earlier in the day.
        assert 18 in exit_hours, (
            f"no trade exited at hour 18 — exit gate not firing at "
            f"window-close. exit_hours observed: {sorted(exit_hours)}"
        )
