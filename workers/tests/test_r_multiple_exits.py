"""Primitive-4 — RMultipleExit (fixed risk-reward, ATR-anchored PRIMARY
exit) schema + math + drift-parity tests.

RMultipleExit is a NEW ExitCondition WRAPPER (type-discriminated, not a
StopLossMethod / TakeProfitMethod member). It defines BOTH a protective
stop and a profit target in one object via a fixed risk-reward ratio:

    R      = atr_multiple × ATR(atr_period)   (measured at entry bar)
    stop   = entry − stop_R   × R
    target = entry + target_R × R

The engines do NOT add a new stop/tp dispatcher branch — instead each
engine's exit-compilation (`_compile_exits` in translator, `_compile_exit_rules`
in iterative) calls `decompose_r_multiple` to synthesize a
StopLossAtrMultiple(mult = stop_R × atr_multiple) + a
TakeProfitAtrMultiple(mult = target_R × atr_multiple). The synthesized
methods then flow through the existing ATR-multiple stop/tp code paths
unchanged.

PRIMARY exit vs signal exit: unlike the existing signal-exit strategies
(condition-type exits that close on an indicator flip at bar CLOSE), an
r_multiple exit is meant to HIT either its stop or target INTRABAR. It is
the strategy's core risk-management mechanic, not an auxiliary trend-flip
exit. The tests below exercise the intrabar fill (stop-before-target
priority) directly.

EMPIRICAL-INSPECTION META-LESSON (v1.2 standing rule): every numeric
literal asserted below was first PRINTED from a real engine run, then
HAND-VERIFIED by the R arithmetic in the test docstrings, THEN encoded.
The literals are NOT copied from engine output uncritically — each is
independently derivable from (entry_fill, ATR, stop_R, target_R, slippage).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import (
    ExitRules,
    RMultipleExit,
    StopLossAtrMultiple,
    StrategySpec,
    TakeProfitAtrMultiple,
    Timeframe,
    decompose_r_multiple,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest

_START = datetime(2024, 1, 1, tzinfo=UTC)
_END = datetime(2030, 1, 1, tzinfo=UTC)


# ---- fixtures --------------------------------------------------------------


def _flat_then_uptrend(n: int = 50) -> pd.DataFrame:
    """30 flat bars at close=100 with true range = 2 (high 101, low 99),
    so Wilder's ATR(14) converges EXACTLY to 2.0 by bar 14+. Then a slow,
    monotone climb so an entry filling post-warmup will hit its
    target_R × R target.

    The entry condition `close >= 100.5` first becomes true at bar 30 (the
    first non-flat close). Its fill is the NEXT open (bar 31 open = 100.5),
    and ATR at the SIGNAL bar (30) is still exactly 2.0 (bar 30's TR is 2),
    making every R-multiple level hand-computable.
    """
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for i in range(n):
        if i < 30:
            o, h, low, c = 100.0, 101.0, 99.0, 100.0
        elif i == 30:
            o, h, low, c = 100.0, 101.0, 99.0, 100.5
        else:
            c = 100.5 + 1.0 * (i - 30)
            o = closes[-1]
            h = c + 0.5
            low = c - 0.5
        opens.append(o)
        highs.append(h)
        lows.append(low)
        closes.append(c)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=n, freq="1h")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1e6] * n},
        index=idx,
    )


def _gap_through_both() -> pd.DataFrame:
    """Same flat warmup + a single entry at bar 30 (fill at bar 31 open
    100.5). Bar 32 GAPS THROUGH BOTH the stop (98.55) and the target
    (106.55): low=90 (≤ stop) AND high=110 (≥ target). The intrabar
    fill-priority rule (_intrabar_exit checks STOP before TARGET) must
    resolve this to a stop_loss exit.
    """
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    for i in range(40):
        if i < 30:
            o, h, low, c = 100.0, 101.0, 99.0, 100.0
        elif i == 30:
            o, h, low, c = 100.0, 101.0, 99.0, 100.5
        elif i == 31:
            o, h, low, c = 100.5, 101.0, 100.0, 100.5  # entry fills here
        elif i == 32:
            o, h, low, c = 100.5, 110.0, 90.0, 100.0  # gap through BOTH
        else:
            o, h, low, c = 100.0, 101.0, 99.0, 100.0
        opens.append(o)
        highs.append(h)
        lows.append(low)
        closes.append(c)
    idx = pd.date_range("2024-01-01 00:00:00+00:00", periods=40, freq="1h")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": [1e6] * 40},
        index=idx,
    )


def _spec(
    *,
    atr_period: int = 14,
    atr_multiple: float = 1.0,
    stop_R: float = 1.0,
    target_R: float = 3.0,
    direction: str = "long",
    entry_threshold: float = 100.5,
) -> StrategySpec:
    spec_dict: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "r-multiple exit",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": direction,
        "entry": {
            "condition": {
                "type": "compare",
                "left": {"kind": "price", "field": "close"},
                "op": ">=",
                "right": {"kind": "constant", "value": entry_threshold},
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {
                    "type": "r_multiple",
                    "atr_period": atr_period,
                    "atr_multiple": atr_multiple,
                    "stop_R": stop_R,
                    "target_R": target_R,
                },
            ],
        },
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
    }
    spec, _warnings = validate_spec(spec_dict)
    return spec


# ---- schema ----------------------------------------------------------------


class TestRMultipleExitSchema:
    def test_basic_construction_and_defaults(self) -> None:
        ex = RMultipleExit()
        assert ex.type == "r_multiple"
        assert ex.atr_period == 14
        assert ex.atr_multiple == 1.0
        assert ex.stop_R == 1.0
        assert ex.target_R == 3.0

    def test_explicit_values(self) -> None:
        ex = RMultipleExit(atr_period=20, atr_multiple=2.0, stop_R=1.5, target_R=4.5)
        assert ex.atr_period == 20
        assert ex.atr_multiple == 2.0
        assert ex.stop_R == 1.5
        assert ex.target_R == 4.5

    @pytest.mark.parametrize("bad", [0, 1, 101, 200, -1])
    def test_atr_period_bounds_rejected(self, bad: int) -> None:
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            RMultipleExit(atr_period=bad)

    @pytest.mark.parametrize("bad", [-1.0, 0.0, 20.001, 100.0])
    def test_atr_multiple_bounds_rejected(self, bad: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            RMultipleExit(atr_multiple=bad)

    @pytest.mark.parametrize("bad", [-1.0, 0.0, 100.001, 200.0])
    def test_stop_R_bounds_rejected(self, bad: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            RMultipleExit(stop_R=bad)

    @pytest.mark.parametrize("bad", [-1.0, 0.0, 100.001, 200.0])
    def test_target_R_bounds_rejected(self, bad: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            RMultipleExit(target_R=bad)

    def test_round_trip_preserves_equality(self) -> None:
        ex = RMultipleExit(atr_period=21, atr_multiple=1.5, stop_R=2.0, target_R=6.0)
        rt = RMultipleExit.model_validate_json(ex.model_dump_json())
        assert rt == ex

    def test_routes_via_exit_condition_discriminator(self) -> None:
        """The type-discriminated ExitCondition union (NOT the kind-
        discriminated StopLossMethod/TakeProfitMethod) picks up the new
        type='r_multiple' wrapper.
        """
        rules = ExitRules.model_validate(
            {
                "exits": [
                    {
                        "type": "r_multiple",
                        "atr_period": 14,
                        "atr_multiple": 1.0,
                        "stop_R": 1.0,
                        "target_R": 3.0,
                    },
                ],
            },
        )
        assert len(rules.exits) == 1
        assert isinstance(rules.exits[0], RMultipleExit)

    def test_validate_spec_accepts_r_multiple_only_exit(self) -> None:
        """A spec whose ONLY exit is an r_multiple validates cleanly —
        r_multiple supplies both the protective stop and the target, so
        the spec is not "stop-less"."""
        spec = _spec()
        assert isinstance(spec.exit.exits[0], RMultipleExit)


class TestDecomposeRMultiple:
    """The single source of truth both engines call. R = atr_multiple ×
    ATR; stop_distance = stop_R × R; target_distance = target_R × R, so
    the synthesized ATR-multiple mults are (stop_R × atr_multiple) and
    (target_R × atr_multiple).
    """

    def test_classic_1to3(self) -> None:
        stop, tp = decompose_r_multiple(
            RMultipleExit(atr_period=14, atr_multiple=1.0, stop_R=1.0, target_R=3.0),
        )
        assert isinstance(stop, StopLossAtrMultiple)
        assert isinstance(tp, TakeProfitAtrMultiple)
        assert stop.atr_period == 14
        assert tp.atr_period == 14
        assert stop.mult == 1.0
        assert tp.mult == 3.0

    def test_atr_multiple_scales_both_legs(self) -> None:
        # atr_multiple=2 doubles 1 R, so stop mult = 1×2, target mult = 3×2.
        stop, tp = decompose_r_multiple(
            RMultipleExit(atr_period=10, atr_multiple=2.0, stop_R=1.0, target_R=3.0),
        )
        assert stop.mult == 2.0
        assert tp.mult == 6.0

    def test_synthesized_mult_clamped_to_schema_bound(self) -> None:
        # stop_R=100, atr_multiple=20 -> 2000, clamped to the
        # StopLossAtrMultiple.mult bound of 20 so the synthesized method
        # stays schema-valid. Far past any realistic R:R.
        stop, tp = decompose_r_multiple(
            RMultipleExit(atr_period=14, atr_multiple=20.0, stop_R=100.0, target_R=100.0),
        )
        assert stop.mult == 20.0
        assert tp.mult == 20.0


# ---- LONG iterative stop / target math (empirical, hand-verified) ----------


class TestIterativeLongMath:
    """PRIMARY-exit math on the iterative (long-only) engine.

    Hand-verified setup (printed from a real run, then derived):
      - Flat warmup ⇒ ATR(14) = 2.0 exactly at the signal bar (30).
      - Entry signal at bar 30 (close 100.5 ≥ 100.5); fills at bar 31's
        open = 100.5. Taker slippage = 5 bps, so
            entry_fill = 100.5 × 1.0005 = 100.55025.
      - R = atr_multiple × ATR = 1.0 × 2.0 = 2.0.
      - target = entry_fill + target_R × R = 100.55025 + 3 × 2.0 = 106.55025.
      - On the target-hit bar the fill is at the target level (not a gap),
        exit_fill = 106.55025 × (1 − 0.0005) = 106.49697487499999.
    """

    def test_first_trade_hits_target_at_hand_verified_price(self) -> None:
        spec = _spec()
        data = _flat_then_uptrend(50)
        run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert run.trades, "no trades fired"
        t = run.trades[0]
        # entry_time is the SIGNAL bar (30), per the iterative convention.
        assert data.index.get_loc(t.entry_time) == 30
        # entry_fill = 100.5 × 1.0005 (next-open fill + taker slippage).
        assert t.entry_price == pytest.approx(100.55025, abs=1e-9)
        assert t.exit_reason == "take_profit"
        # exit_fill = target × (1 − slippage) = 106.55025 × 0.9995.
        assert t.exit_price == pytest.approx(106.49697487499999, abs=1e-6)
        # Independent cross-check: the gross target = entry_fill + 3×R.
        expected_target = 100.55025 + 3.0 * (1.0 * 2.0)
        assert t.exit_price == pytest.approx(expected_target * (1 - 0.0005), abs=1e-6)

    def test_stop_R_widens_the_stop_distance(self) -> None:
        """A wider stop_R does not change the LONG target on this fixture
        (the climb still hits the target first), but the synthesized stop
        mult must reflect stop_R × atr_multiple.
        """
        stop, _tp = decompose_r_multiple(RMultipleExit(stop_R=2.0))
        assert stop.mult == 2.0  # 2.0 × 1.0


# ---- gap-through-BOTH intrabar priority -----------------------------------


class TestGapThroughBoth:
    """The contract's load-bearing edge case: a single bar whose
    low ≤ stop AND high ≥ target. _intrabar_exit checks STOP before
    TARGET, so the conservative resolution is a stop_loss exit.

    Hand-verified: entry_fill = 100.55025, ATR = 2.0, stop_R = 1 ⇒
    stop = 100.55025 − 1×2.0 = 98.55025; target = 106.55025. Bar 32 gaps
    open=100.5, low=90, high=110 — through both. open (100.5) > stop
    (98.55025), so the fill is AT the stop level (you can fill at the
    stop, the gap didn't open below it): exit_fill = 98.55025 × 0.9995
    = 98.500974875.
    """

    def test_stop_wins_over_target_on_gap_bar(self) -> None:
        spec = _spec()
        data = _gap_through_both()
        run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        assert len(run.trades) == 1
        t = run.trades[0]
        assert t.exit_reason == "stop_loss", (
            f"gap through both levels resolved to {t.exit_reason}; the "
            f"stop-before-target intrabar priority is broken"
        )
        # Exit fills at the stop level (open 100.5 did not gap below it).
        assert t.exit_price == pytest.approx(98.500974875, abs=1e-6)
        expected_stop = 100.55025 - 1.0 * (1.0 * 2.0)
        assert t.exit_price == pytest.approx(expected_stop * (1 - 0.0005), abs=1e-6)
        # The exit lands on the gap bar (32), intrabar.
        assert data.index.get_loc(t.exit_time) == 32


# ---- cross-engine drift parity (envelope) ---------------------------------


class TestCrossEngineDriftParity:
    """RMultipleExit synthesizes the SAME StopLossAtrMultiple +
    TakeProfitAtrMultiple in BOTH engines, so they share the schema-level
    exit definition. They are NOT bit-identical at the trade-ledger level:
    vbt's from_signals resolves a same-bar stop/target tie by its own
    internal ordering, while the iterative engine applies an explicit
    stop-before-target rule (_intrabar_exit), and vbt's percent-of-close
    stop fraction differs from the iterative engine's absolute at-entry
    ATR distance by rounding. The ±2× trade-count envelope is the correct
    gate for a purely-additive primitive (same finding as v1.2.A/E).
    """

    def test_iterative_vs_vbt_long_envelope(self) -> None:
        spec = _spec()
        data = _flat_then_uptrend(120)
        it_run = run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        vbt_run = run_backtest(
            spec, _START, _END, 10_000.0, data_override={Timeframe.H1: data},
        )
        assert it_run.trades, "iterative produced no trades"
        assert vbt_run.trades, "vbt produced no trades"
        ratio = len(vbt_run.trades) / len(it_run.trades)
        assert 0.5 <= ratio <= 2.0, (
            f"vbt={len(vbt_run.trades)} iterative={len(it_run.trades)} "
            f"ratio={ratio:.2f} — outside the ±2× cross-engine envelope"
        )

    def test_vbt_short_smoke(self) -> None:
        """SHORT runs through vbt only — the iterative engine is long-only
        and must reject SHORT cleanly. vbt handles SHORT via
        direction='shortonly', flipping the sign on the (positive)
        synthesized stop/tp fractions internally.
        """
        spec = _spec(direction="short")
        data = _flat_then_uptrend(120)
        with pytest.raises(Exception, match="long"):
            run_iterative_backtest(spec, {Timeframe.H1: data}, _START, _END, 10_000.0)
        # vbt path must run without R-multiple-synthesis errors.
        vbt_run = run_backtest(
            spec, _START, _END, 10_000.0, data_override={Timeframe.H1: data},
        )
        assert vbt_run is not None
