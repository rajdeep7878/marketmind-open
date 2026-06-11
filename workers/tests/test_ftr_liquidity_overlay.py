"""Liquidity overlay: blocks/defers entries only, never exits, never
initiates; Abdi-Ranaldo estimator sanity; DEFER->SKIP escalation."""

from __future__ import annotations

import pandas as pd
from marketmind_workers.ftr.strategies.liquidity_overlay import (
    LiquidityOverlay,
    OverlayDecision,
    abdi_ranaldo_spread_bps,
    apply_overlay_to_positions,
    hour_of_week_liquidity_score,
)
from marketmind_workers.ftr.strategies.specs import LiquidityOverlaySpec

from .ftr_helpers import synthetic_ohlcv


def test_abdi_ranaldo_positive_and_scales_with_range() -> None:
    calm = synthetic_ohlcv(n_bars=2000, seed=61, vol=0.001)
    wild = synthetic_ohlcv(n_bars=2000, seed=61, vol=0.01)
    s_calm = abdi_ranaldo_spread_bps(calm).dropna()
    s_wild = abdi_ranaldo_spread_bps(wild).dropna()
    assert (s_calm >= 0).all()
    assert s_wild.mean() > s_calm.mean()


def test_liquidity_score_in_unit_interval() -> None:
    df = synthetic_ohlcv(n_bars=24 * 60, seed=62)
    score = hour_of_week_liquidity_score(df["volume"])
    assert float(score.min()) >= 0.0
    assert float(score.max()) <= 1.0


def _overlay(*, always_bad: bool, n: int = 500) -> LiquidityOverlay:
    idx = pd.date_range("2026-01-01", periods=n, freq="1h", tz="UTC")
    if always_bad:
        # strictly rising spread: every 'current' reading is the worst in
        # its own trailing window => percentile ~1.0 => never ALLOW
        spread = pd.Series(1.0 + 0.01 * pd.RangeIndex(n).to_numpy(), index=idx)
    else:
        spread = pd.Series(1.0, index=idx)
    score = pd.Series(1.0, index=idx)
    return LiquidityOverlay(
        LiquidityOverlaySpec(max_defer_bars=2), spread_bps=spread, liquidity_score=score
    )


def test_overlay_never_touches_exits_and_never_initiates() -> None:
    idx = pd.date_range("2026-02-01", periods=20, freq="1h", tz="UTC")
    # raw strategy wants: flat(5), long(10), flat(5)
    raw = pd.Series([0] * 5 + [1] * 10 + [0] * 5, index=idx, dtype="int64")
    overlay = _overlay(always_bad=False, n=2000)
    shifted, _ = apply_overlay_to_positions(raw, overlay)
    # ALLOW-everything overlay: positions unchanged
    assert (shifted == raw).all()
    # an overlay can only ever produce a subset of long bars, never new ones
    overlay_bad = _overlay(always_bad=True, n=2000)
    blocked, _log = apply_overlay_to_positions(raw, overlay_bad)
    assert (blocked <= raw).all()


def test_defer_then_skip_escalation() -> None:
    overlay = _overlay(always_bad=True, n=3000)
    ts = pd.Timestamp("2026-01-30 12:00", tz="UTC")
    verdicts = [overlay.evaluate(ts + pd.Timedelta(hours=i)).decision for i in range(3)]
    assert verdicts == [OverlayDecision.DEFER, OverlayDecision.DEFER, OverlayDecision.SKIP]


def test_missing_measurement_allows() -> None:
    """No spread data = no information = ALLOW (the overlay must never
    invent a veto from missing input)."""
    idx = pd.date_range("2026-01-01", periods=10, freq="1h", tz="UTC")
    overlay = LiquidityOverlay(
        LiquidityOverlaySpec(),
        spread_bps=pd.Series(dtype="float64"),
        liquidity_score=pd.Series(1.0, index=idx),
    )
    verdict = overlay.evaluate(pd.Timestamp("2026-01-05", tz="UTC"))
    assert verdict.decision == OverlayDecision.ALLOW
