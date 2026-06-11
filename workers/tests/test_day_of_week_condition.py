"""v1.2.D — DayOfWeekCondition (day-of-week entry/exit gate) tests.

Direct copy-paste of v1.2.C's TimeOfDayCondition test structure with
evaluator swap from .hour to .weekday. Same META-LESSON applied:
empirical inspection before encoding end-to-end assertions.

Empirical pre-test inspection (28-bar daily fixture starting on
Monday 2024-01-01, weekdays=[0,1,2,3,4] entry gate, NOT-gate exit):
  - Entries all land on Monday (weekday=0) — the spec re-opens on
    the first allowed weekday after a prior exit.
  - Exits all land on Saturday (weekday=5) — the first bar where
    NOT(weekdays in [0..4]) is True.
  - Confirms .weekday gating works end-to-end.

Four test classes:
  1. TestDayOfWeekConditionSchema — validation: bounds, duplicates,
     empty rejection, round-trip, discriminator routing.
  2. TestEvalDayOfWeek — pure unit tests on _eval_day_of_week:
     weekdays-only, weekend-only, single-day, full-week, isolated
     days like [4] (Friday) or [2] (Wednesday).
  3. TestDispatcherIdentity — _eval_condition_on_tf routes
     DayOfWeekCondition to the same _eval_day_of_week helper that's
     importable directly.
  4. TestEndToEnd — minimal spec with DayOfWeekCondition gating an
     entry runs through both vbt and iterative paths; entries land
     on configured weekdays.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import (
    DayOfWeekCondition,
    StrategySpec,
    Timeframe,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


# ---- Schema validation ----------------------------------------------------


class TestDayOfWeekConditionSchema:
    def test_basic_construction(self) -> None:
        c = DayOfWeekCondition(weekdays=[0, 1, 2, 3, 4])
        assert c.type == "day_of_week"
        assert c.weekdays == [0, 1, 2, 3, 4]

    def test_single_day(self) -> None:
        c = DayOfWeekCondition(weekdays=[4])
        assert c.weekdays == [4]

    def test_empty_weekdays_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            DayOfWeekCondition(weekdays=[])

    @pytest.mark.parametrize("bad_weekdays", [
        [-1], [7], [0, 1, 8], [10], [-5, 0],
    ])
    def test_out_of_range_weekday_rejected(self, bad_weekdays: list[int]) -> None:
        with pytest.raises(Exception):  # noqa: B017
            DayOfWeekCondition(weekdays=bad_weekdays)

    @pytest.mark.parametrize("dup_weekdays", [
        [0, 0], [1, 2, 1], [0, 1, 2, 3, 4, 5, 6, 0],
    ])
    def test_duplicate_weekday_rejected(self, dup_weekdays: list[int]) -> None:
        # max_length=7 also bounds the [0,1,2,3,4,5,6,0] case.
        with pytest.raises(Exception):  # noqa: B017
            DayOfWeekCondition(weekdays=dup_weekdays)

    def test_all_seven_weekdays_accepted(self) -> None:
        # Entire week — degenerate "always True" but valid.
        c = DayOfWeekCondition(weekdays=[0, 1, 2, 3, 4, 5, 6])
        assert c.weekdays == [0, 1, 2, 3, 4, 5, 6]

    def test_round_trip_preserves_equality(self) -> None:
        c = DayOfWeekCondition(weekdays=[5, 6])
        roundtripped = DayOfWeekCondition.model_validate_json(c.model_dump_json())
        assert roundtripped == c

    def test_routes_via_condition_discriminator(self) -> None:
        from marketmind_shared.schemas.strategy_spec import Condition
        from pydantic import TypeAdapter

        adapter = TypeAdapter(Condition)
        parsed = adapter.validate_python(
            {"type": "day_of_week", "weekdays": [0, 1, 2, 3, 4]},
        )
        assert isinstance(parsed, DayOfWeekCondition)
        assert parsed.weekdays == [0, 1, 2, 3, 4]


# ---- _eval_day_of_week helper unit tests ----------------------------------


def _daily_df(days: int = 14) -> pd.DataFrame:
    """N-day UTC-aware daily DataFrame starting on Monday 2024-01-01."""
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=days, freq="1D")
    return pd.DataFrame(
        {
            "open": [1.0] * days,
            "high": [1.0] * days,
            "low": [1.0] * days,
            "close": [1.0] * days,
            "volume": [1.0] * days,
        },
        index=idx,
    )


class TestEvalDayOfWeek:
    @staticmethod
    def _eval(cond: DayOfWeekCondition, df: pd.DataFrame) -> pd.Series:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _eval_day_of_week,
        )

        return _eval_day_of_week(cond, df)

    def test_weekdays_only_excludes_weekends(self) -> None:
        # 14 days starting Mon -> 10 weekdays (Mon-Fri x 2) + 4 weekend days
        c = DayOfWeekCondition(weekdays=[0, 1, 2, 3, 4])
        mask = self._eval(c, _daily_df(days=14))
        assert mask.sum() == 10

    def test_weekends_only(self) -> None:
        # 14 days -> 4 weekend days (Sat x 2 + Sun x 2)
        c = DayOfWeekCondition(weekdays=[5, 6])
        mask = self._eval(c, _daily_df(days=14))
        assert mask.sum() == 4

    def test_single_weekday_friday(self) -> None:
        # Friday-only over 14 days -> 2 Fridays (weekday=4)
        c = DayOfWeekCondition(weekdays=[4])
        mask = self._eval(c, _daily_df(days=14))
        assert mask.sum() == 2

    def test_single_weekday_monday(self) -> None:
        # Monday-only -> 2 Mondays in 14 days
        c = DayOfWeekCondition(weekdays=[0])
        mask = self._eval(c, _daily_df(days=14))
        assert mask.sum() == 2

    def test_full_week_is_always_true(self) -> None:
        c = DayOfWeekCondition(weekdays=[0, 1, 2, 3, 4, 5, 6])
        mask = self._eval(c, _daily_df(days=14))
        assert mask.sum() == 14
        assert mask.all()

    def test_arbitrary_subset(self) -> None:
        # Mon + Wed + Fri -> 6 bars in 14 days
        c = DayOfWeekCondition(weekdays=[0, 2, 4])
        mask = self._eval(c, _daily_df(days=14))
        assert mask.sum() == 6

    def test_weekday_indexing_matches_pandas_convention(self) -> None:
        # 2024-01-01 is a Monday. weekday() == 0. Confirm the engine
        # uses pandas .weekday (Monday=0), not the Python .isoweekday
        # (Monday=1) or any other ordering.
        c = DayOfWeekCondition(weekdays=[0])
        mask = self._eval(c, _daily_df(days=7))
        true_dates = mask[mask].index.tolist()  # type: ignore[index]
        assert len(true_dates) == 1
        assert true_dates[0].weekday() == 0  # Monday


# ---- Dispatcher identity --------------------------------------------------


class TestDispatcherIdentity:
    def test_dispatcher_output_matches_helper_call(self) -> None:
        from marketmind_workers.backtest.translator import (  # type: ignore[attr-defined]
            _Context,
            _eval_condition_on_tf,
            _eval_day_of_week,
        )

        data = _daily_df(days=14)
        ctx = _Context(spec=None, data={Timeframe.D1: data}, primary_index=data.index)  # type: ignore[arg-type]
        cond = DayOfWeekCondition(weekdays=[0, 2, 4])

        via_dispatcher = _eval_condition_on_tf(cond, ctx, timeframe=Timeframe.D1)
        via_helper = _eval_day_of_week(cond, data)

        pd.testing.assert_series_equal(via_dispatcher, via_helper)


# ---- End-to-end ----------------------------------------------------------


def _synthetic_daily_uptrend(days: int = 28) -> pd.DataFrame:
    """N-day UTC-aware uptrend starting on Monday 2024-01-01.
    Linear with mild noise — enough variance for trades to fire AND
    exit cleanly within the day-of-week gate."""
    rng = np.random.default_rng(13)
    closes = 100 + 0.5 * np.arange(days) + rng.normal(0, 0.3, days)
    closes = np.maximum(closes, 1.0)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=days, freq="1D")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 0.5,
            "low": closes - 0.5,
            "close": closes,
            "volume": np.full(days, 1e6),
        },
        index=idx,
    )


def _spec_weekday_only_gated() -> StrategySpec:
    """Enter long on weekdays only (Mon-Fri), exit on weekends.
    Always-True compare gates by weekday alone."""
    spec_dict: dict[str, Any] = {
        "schema_version": "2.0",
        "name": "Day-of-week gated",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1d",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "and",
                "conditions": [
                    {
                        "type": "compare",
                        "left": {"kind": "price", "field": "close"},
                        "op": ">",
                        "right": {"kind": "constant", "value": 0.0},
                    },
                    {"type": "day_of_week", "weekdays": [0, 1, 2, 3, 4]},
                ],
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.5}},
                {
                    "type": "condition",
                    "condition": {
                        "type": "not",
                        "condition": {
                            "type": "day_of_week",
                            "weekdays": [0, 1, 2, 3, 4],
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
    def test_iterative_entries_land_on_weekdays(self) -> None:
        """Every trade's entry_time.weekday() is in [0,1,2,3,4]. Per
        the empirical inspection in the module docstring, entries
        typically land on Monday (the first allowed weekday after a
        prior exit), but Tue-Fri are also valid landing spots
        depending on the prior exit's timing.
        """
        spec = _spec_weekday_only_gated()
        data = _synthetic_daily_uptrend(28)
        run = run_iterative_backtest(spec, {Timeframe.D1: data}, _START, _END, 10_000.0)
        assert len(run.trades) > 0
        for trade in run.trades:
            wd = trade.entry_time.weekday()
            assert 0 <= wd <= 4, (
                f"trade entered on weekday {wd} — outside the [0,1,2,3,4] "
                f"gate the day_of_week condition is supposed to enforce"
            )

    def test_iterative_exits_land_on_weekends(self) -> None:
        """The NOT(day_of_week in [0..4]) exit forces a close at the
        first bar outside the gate — i.e. Saturday (weekday=5)."""
        spec = _spec_weekday_only_gated()
        data = _synthetic_daily_uptrend(28)
        run = run_iterative_backtest(spec, {Timeframe.D1: data}, _START, _END, 10_000.0)
        assert len(run.trades) > 0
        exit_weekdays = {t.exit_time.weekday() for t in run.trades}
        # Most exits at Saturday (5). Allow some slack for stop-loss
        # exits (none on this fixture — 50% stop is too far) — but the
        # primary exit weekday must be 5 (Saturday).
        assert 5 in exit_weekdays, (
            f"no trade exited on Saturday — exit gate not firing at the "
            f"first weekend bar. exit_weekdays observed: {sorted(exit_weekdays)}"
        )

    def test_vbt_and_iterative_both_run(self) -> None:
        """Both engines run a day_of_week-gated spec without errors.
        Same ±2× envelope as v1.2.A/C — shared dispatcher means the
        entry signals match in count even though exit-tie-break
        differs."""
        spec = _spec_weekday_only_gated()
        data = _synthetic_daily_uptrend(60)  # more bars = more trades
        vbt_run = run_backtest(spec, _START, _END, 10_000.0, data_override={Timeframe.D1: data})
        it_run = run_iterative_backtest(spec, {Timeframe.D1: data}, _START, _END, 10_000.0)
        assert len(vbt_run.trades) > 0
        assert len(it_run.trades) > 0
        ratio = len(vbt_run.trades) / len(it_run.trades)
        assert 0.5 <= ratio <= 2.0
