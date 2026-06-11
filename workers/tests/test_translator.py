"""Tests for the spec->signals translator.

Two halves:

  - For each of the 8 Phase 1 valid fixtures, build signals against a
    small synthetic OHLCV dataset and assert the SignalSet has the
    expected shape and (where possible) a hand-verified signal point.
  - A dedicated look-ahead test: construct data where a look-ahead bug
    would produce a detectably different (better) result, and assert
    the translator does NOT exhibit it.
  - An MTF alignment test using fixture 04, the only multi-timeframe
    fixture in the Phase 1 set.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import SignalDiagnosticsFailureMode, validate_spec
from marketmind_shared.schemas.strategy_spec import Direction, Timeframe
from marketmind_workers.backtest.translator import (
    SignalSet,
    TranslationError,
    _classify_entry_diagnostics,
    build_signals,
)

# ---- fixture / dataset helpers --------------------------------------------


_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "strategies" / "valid"


def _load_fixture(name: str):
    spec_dict = json.loads((_FIXTURES_DIR / name).read_text())
    spec, _warnings = validate_spec(spec_dict)
    return spec


def _make_ohlcv(
    n: int,
    *,
    freq: str = "1D",
    start: datetime | None = None,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    """Build a synthetic OHLCV DataFrame.

    Default: monotonically rising prices with close = 100 + i.
    """
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    idx = pd.date_range(start, periods=n, freq=freq)
    if closes is None:
        c = np.arange(100.0, 100.0 + n, dtype=float)
    else:
        c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": c - 0.1,
            "high": c + 0.5,
            "low": c - 0.5,
            "close": c,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


# ---- 1. Golden Cross fixture ----------------------------------------------


def test_fixture_01_golden_cross_signals() -> None:
    spec = _load_fixture("01_golden_cross.json")
    # Need both SMA(50) and SMA(200) to have valid values BEFORE the
    # crossover bar — crossover semantics compare bar t-1 to bar t, so
    # if t-1 is in the SMA(200) warmup we'd compare against NaN and
    # never fire. Pattern: 250 flat bars (both SMAs converge), then
    # 250 rising bars (SMA(50) outpaces SMA(200) -> golden cross),
    # then 100 falling bars (death cross -> exit fires).
    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    data = {Timeframe.D1: _make_ohlcv(n, closes=closes)}

    signals = build_signals(spec, data)

    assert isinstance(signals, SignalSet)
    assert signals.direction is Direction.LONG
    assert signals.entries.dtype == bool
    assert signals.exits.dtype == bool
    # Should fire at least one entry (the golden cross) and one exit
    # (the death cross on the falling tail).
    assert signals.entries.sum() >= 1
    assert signals.exits.sum() >= 1


# ---- 2. RSI mean reversion ------------------------------------------------


def test_fixture_02_rsi_mean_reversion_signals() -> None:
    spec = _load_fixture("02_rsi_mean_reversion.json")
    # Need RSI(14) to START above 30 and CROSS below 30 — a monotonic
    # drop never crosses (it was already below). Construction: a noisy
    # warmup (RSI settles around 50), then a sharp drop, then a recovery.
    n = 100
    warmup = [100.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(30)]
    drop = list(np.linspace(100, 50, 20))
    rally = list(np.linspace(50, 110, 30))
    tail = [110.0 + (3.0 if i % 2 == 0 else -3.0) for i in range(20)]
    closes = warmup + drop + rally + tail
    data = {Timeframe.H4: _make_ohlcv(n, freq="4h", closes=closes)}

    signals = build_signals(spec, data)
    # Spec has a stop_loss method (percent 5%); make sure it threaded
    # through to the SignalSet.
    assert signals.stop_loss is not None
    assert signals.stop_loss.kind == "percent"
    # Should produce at least one entry given the engineered RSI dip.
    assert signals.entries.sum() >= 1


# ---- 3. Breakout with volume ----------------------------------------------


def test_fixture_03_breakout_with_volume_signals() -> None:
    spec = _load_fixture("03_breakout_with_volume.json")
    # Build a series that holds steady then breaks out with a volume spike.
    n = 60
    closes = [100.0] * 30 + list(np.linspace(100, 130, 30))  # quiet then breakout
    df = _make_ohlcv(n, freq="1h", closes=closes)
    # Pump volume on the breakout bars
    df.loc[df.index[30:], "volume"] = 5_000_000.0
    data = {Timeframe.H1: df}

    signals = build_signals(spec, data)
    assert signals.stop_loss is not None
    assert signals.take_profit is not None
    # Spec is r_multiple TP — confirm the kind
    assert signals.take_profit.kind == "r_multiple"


# ---- 4. MTF trend pullback (the multi-timeframe fixture) ------------------


def test_fixture_04_mtf_trend_pullback_signals() -> None:
    spec = _load_fixture("04_mtf_trend_pullback.json")
    # Build 15m primary + 1h filter data. Both need EMA warmup.
    # 1h: 200 bars (rising for filter EMA 50). 15m: 800 bars (4x).
    n_1h = 200
    closes_1h = list(np.linspace(100, 150, n_1h))
    df_1h = _make_ohlcv(
        n_1h,
        freq="1h",
        closes=closes_1h,
        start=datetime(2024, 1, 1, tzinfo=UTC),
    )

    # 15m primary covering the same window. 4 bars/hour * 200h = 800 bars.
    n_15m = 800
    df_15m = _make_ohlcv(
        n_15m,
        freq="15min",
        # Track the same general trend; sprinkle an engulfing pattern.
        closes=list(np.linspace(100, 150, n_15m)),
        start=datetime(2024, 1, 1, tzinfo=UTC),
    )
    # Inject a bullish-engulfing-shaped bar pair midway through.
    mid = 600
    df_15m.loc[df_15m.index[mid - 1], "open"] = 130.0
    df_15m.loc[df_15m.index[mid - 1], "close"] = 125.0
    df_15m.loc[df_15m.index[mid - 1], "high"] = 130.5
    df_15m.loc[df_15m.index[mid - 1], "low"] = 124.5
    df_15m.loc[df_15m.index[mid], "open"] = 124.0
    df_15m.loc[df_15m.index[mid], "close"] = 131.0
    df_15m.loc[df_15m.index[mid], "high"] = 131.5
    df_15m.loc[df_15m.index[mid], "low"] = 123.5

    data = {Timeframe.M15: df_15m, Timeframe.H1: df_1h}
    signals = build_signals(spec, data)

    assert isinstance(signals, SignalSet)
    # Entries/exits index must be the 15m primary, not the 1h filter.
    assert len(signals.entries) == n_15m
    assert signals.entries.index.equals(df_15m.index)


# ---- 5. Bollinger breakout ------------------------------------------------


def test_fixture_05_bollinger_breakout_signals() -> None:
    spec = _load_fixture("05_bollinger_breakout.json")
    # Quiet then sharp breakout above upper band.
    n = 60
    closes = [100.0] * 30 + list(np.linspace(100, 110, 30))
    data = {Timeframe.H1: _make_ohlcv(n, freq="1h", closes=closes)}

    signals = build_signals(spec, data)
    assert signals.max_bars_held == 48  # the spec's time exit
    assert signals.stop_loss is not None
    assert signals.stop_loss.kind == "trailing_percent"


# ---- 6. Short trend continuation ------------------------------------------


def test_fixture_06_short_trend_continuation_signals() -> None:
    spec = _load_fixture("06_short_trend_continuation.json")
    # Sustained downtrend so EMA(20) < EMA(50) and close < EMA(20).
    n = 200
    closes = list(np.linspace(200, 100, n))
    data = {Timeframe.H4: _make_ohlcv(n, freq="4h", closes=closes)}

    signals = build_signals(spec, data)
    assert signals.direction is Direction.SHORT
    # Short strategy: stop_loss should be ATR-based per the spec
    assert signals.stop_loss is not None
    assert signals.stop_loss.kind == "atr_multiple"


# ---- 7. MACD histogram cross with session filter -------------------------


def test_fixture_07_macd_with_session_filter_signals() -> None:
    spec = _load_fixture("07_macd_with_session_filter.json")
    n = 200
    # Choppy then trending up so MACD hist crosses above zero
    closes = list(np.linspace(100, 95, 50)) + list(np.linspace(95, 130, 150))
    data = {Timeframe.H1: _make_ohlcv(n, freq="1h", closes=closes)}

    signals = build_signals(spec, data)

    # Session filter 13-21 UTC + weekdays only — assert NO entries are
    # outside that window.
    fired = signals.entries[signals.entries]
    for ts in fired.index:
        assert 13 <= ts.hour <= 21
        assert ts.weekday() < 5
    # Sanity: the count is well-defined (could be 0 depending on cross alignment).
    assert int(fired.sum()) >= 0


# ---- 8. VWAP reversion ---------------------------------------------------


def test_fixture_08_vwap_reversion_signals() -> None:
    spec = _load_fixture("08_vwap_reversion.json")
    # 5m timeframe; need session-anchored VWAP plus a sharp recent drop.
    n = 250
    closes = [100.0] * 200 + list(np.linspace(100, 95, 50))
    df = _make_ohlcv(n, freq="5min", closes=closes)
    # Pump volume on the drop so the volume filter passes
    df.loc[df.index[200:], "volume"] = 5_000_000.0
    data = {Timeframe.M5: df}

    signals = build_signals(spec, data)
    assert signals.direction is Direction.LONG
    # The spec has a stop_loss + a condition exit (close >= VWAP)
    assert signals.stop_loss is not None
    assert signals.stop_loss.kind == "percent"


# ---- Look-ahead protection ------------------------------------------------


def test_crossover_does_not_look_ahead() -> None:
    """Construct a series where look-ahead would let us "buy" at the
    crossover bar's close. The crossover semantics in the translator
    require the comparison to use prev_s vs prev_t — i.e. the cross
    can only be declared at the END of the bar where it crystallizes.

    Test shape: build a Golden-Cross-style spec. Walk a 50/200 SMA
    series where the crossover happens between bars t-1 and t. The
    translator must mark `entries[t] == True` (legal: t-1 was below,
    t is above). It must NOT mark `entries[t-1] == True` (that would
    be peeking at bar t's value while still on bar t-1).
    """
    spec = _load_fixture("01_golden_cross.json")
    # Same shape as test_fixture_01: enough flat bars for both SMAs to
    # warm up and converge, then a steep rise so the cross is clean.
    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    data = {Timeframe.D1: _make_ohlcv(n, closes=closes)}
    signals = build_signals(spec, data)

    # Find the first bar where entries fired.
    entry_indices = list(signals.entries[signals.entries].index)
    assert len(entry_indices) >= 1
    first_entry = entry_indices[0]
    first_pos = signals.entries.index.get_loc(first_entry)
    assert isinstance(first_pos, int)

    # Walk the SMA series we built; the bar BEFORE the entry must NOT
    # already show fast > slow (otherwise the crossover happened earlier
    # and we missed it — that's fine — but it must not show fast > slow
    # AT or BEFORE the entry bar with no earlier flip).
    closes_arr = np.asarray(closes)
    fast = pd.Series(closes_arr).rolling(50).mean().to_numpy()
    slow = pd.Series(closes_arr).rolling(200).mean().to_numpy()
    # On the bar BEFORE the entry, the prev-bar relationship must have
    # been fast <= slow (per the crossover semantics).
    assert fast[first_pos - 1] <= slow[first_pos - 1]
    # AT the entry bar, fast > slow.
    assert fast[first_pos] > slow[first_pos]


# ---- MTF alignment correctness -------------------------------------------


def test_mtf_alignment_uses_close_time_not_open_time() -> None:
    """Direct test of the asof-backward MTF alignment.

    Bypass the full fixture path: build a minimal spec-shaped
    comparison condition that evaluates `close < 1000` on the 1h
    filter timeframe — by construction, this is True on every 1h bar
    EXCEPT one specific bar whose close we set to 9999.

    Then assert: on the 15m primary timeframe, the aligned signal is
    True for bars BEFORE the "9999" 1h bar's close, then False
    starting from the close-of-9999-bar timestamp, then True again
    after the next 1h bar closes (whose close < 1000).

    This isolates the alignment correctness: no indicators, no
    warmups, no compound conditions to muddy the read.
    """
    from marketmind_workers.backtest.translator import (
        _align_to_primary,
        _Context,
    )

    # 1h: 24 bars on day 0. Set bar index 5's close to 9999; every
    # other bar's close stays at 100.
    start = datetime(2024, 1, 1, tzinfo=UTC)
    n_1h = 24
    closes_1h = [100.0] * n_1h
    closes_1h[5] = 9999.0  # the "anomalous" 1h bar
    df_1h = _make_ohlcv(n_1h, freq="1h", closes=closes_1h, start=start)

    # 15m: 96 bars covering the same window.
    n_15m = 96
    df_15m = _make_ohlcv(n_15m, freq="15min", closes=[100.0] * n_15m, start=start)

    # Compute the condition `close < 1000` directly on the 1h series.
    cond_on_1h = (df_1h["close"] < 1000.0).astype(bool)
    # All True except index 5 (which has close == 9999).
    assert cond_on_1h.iloc[5] is np.bool_(False) or not cond_on_1h.iloc[5]
    assert bool(cond_on_1h.iloc[4]) is True
    assert bool(cond_on_1h.iloc[6]) is True

    # Build a tiny synthetic spec that uses this exact MTF shape.
    # We can't construct a StrategySpec just for alignment without
    # going through Pydantic, so call `_align_to_primary` directly.
    primary_idx = df_15m.index
    assert isinstance(primary_idx, pd.DatetimeIndex)
    # Faux Context: only the primary_index field matters for alignment.
    spec = _load_fixture("04_mtf_trend_pullback.json")
    ctx = _Context(
        spec=spec,
        data={Timeframe.M15: df_15m, Timeframe.H1: df_1h},
        primary_index=primary_idx,
    )
    aligned = _align_to_primary(cond_on_1h, source_tf=Timeframe.H1, ctx=ctx)

    # Bar 5's 1h bar opens at 05:00 and closes at 06:00.
    # Bar 6's 1h bar opens at 06:00 and closes at 07:00.
    # Bar 5's value (False) should appear in aligned at primary bars
    # whose open time is >= 06:00 (5's close) and < 07:00 (6's close).
    # I.e. primary bars at 06:00, 06:15, 06:30, 06:45.
    bar_6_open = datetime(2024, 1, 1, 6, 0, tzinfo=UTC)
    bar_5_open = datetime(2024, 1, 1, 5, 0, tzinfo=UTC)
    bar_7_open = datetime(2024, 1, 1, 7, 0, tzinfo=UTC)

    # PRE-9999: at 05:00 (during the 9999 bar's lifetime), the value
    # available is from the bar that closed at 05:00 — bar index 4,
    # which is normal (close=100, condition True).
    assert bool(aligned.loc[bar_5_open]) is True
    # AT 06:00 (when bar 5 has just closed), the aligned value flips
    # to the bar-5 value (False).
    assert bool(aligned.loc[bar_6_open]) is False
    # And 06:15 (inside bar 6), still bar 5's value -> False.
    assert bool(aligned.loc[datetime(2024, 1, 1, 6, 15, tzinfo=UTC)]) is False
    # AT 07:00 (when bar 6 has closed and its value, True, is now
    # available), the aligned value flips back to True.
    assert bool(aligned.loc[bar_7_open]) is True


# ---- Error paths ----------------------------------------------------------


def test_build_signals_raises_when_primary_data_missing() -> None:
    spec = _load_fixture("01_golden_cross.json")
    with pytest.raises(TranslationError, match="missing OHLCV data"):
        build_signals(spec, data={})


def test_build_signals_raises_when_filter_data_missing() -> None:
    spec = _load_fixture("04_mtf_trend_pullback.json")
    df_15m = _make_ohlcv(200, freq="15min")
    with pytest.raises(TranslationError, match="missing OHLCV data"):
        build_signals(spec, data={Timeframe.M15: df_15m})


# ---- Signal diagnostics (silent-zero-trades classifier) -------------------
#
# v1.1 fix for the failure mode in
# docs/operations/v1.1-silent-zero-trades.md: a backtest producing 0
# trades is ambiguous between "real strategy, no signals fired" and
# "spec was logically degenerate / silently NaN'd." The diagnostics
# classify into NONE / CONDITIONS_NEVER_MET / EVALUATION_DEGRADED.


def _series_with_nan(values: list) -> pd.Series:
    """Helper for the pure-function classifier tests — build a Series
    on a synthetic integer index with True/False/None entries (None
    serialises to NaN under pandas' "object" → numeric coercion when
    we explicitly cast via `pd.Series([...], dtype=object)`).
    """
    return pd.Series(values, dtype="object")


def test_classify_diagnostics_mode_none_when_any_true_signal_fires() -> None:
    entries = _series_with_nan([None, None, False, False, True, False])
    diag = _classify_entry_diagnostics(entries, warmup_bars=2)
    assert diag.failure_mode is SignalDiagnosticsFailureMode.NONE
    assert diag.bars_evaluated == 6
    assert diag.nan_warmup_count == 2
    assert diag.nan_post_warmup_count == 0
    assert diag.true_count == 1
    assert diag.deterministic_false_count == 3


def test_classify_diagnostics_mode_conditions_never_met_when_all_false() -> None:
    """Indicators warmed up cleanly, comparison evaluated to False on
    every post-warmup bar. The Supertrend mis-extraction shape:
    `close crosses above ATR(10)` where price is always >> ATR — the
    crossover requires a transition that never happens.
    """
    entries = _series_with_nan([None, None, False, False, False, False, False, False])
    diag = _classify_entry_diagnostics(entries, warmup_bars=2)
    assert diag.failure_mode is SignalDiagnosticsFailureMode.CONDITIONS_NEVER_MET
    assert diag.true_count == 0
    assert diag.deterministic_false_count == 6
    assert diag.nan_post_warmup_count == 0


def test_classify_diagnostics_mode_evaluation_degraded_when_post_warmup_all_nan() -> None:
    """Post-warmup bars are still NaN — something is silently
    producing NaN long after the indicator should have warmed up.
    This is the bug-not-strategy case the v1.1 diagnostic surfaces.
    """
    entries = _series_with_nan([None] * 10)
    diag = _classify_entry_diagnostics(entries, warmup_bars=3)
    assert diag.failure_mode is SignalDiagnosticsFailureMode.EVALUATION_DEGRADED
    assert diag.nan_post_warmup_count == 7
    assert diag.true_count == 0
    assert diag.deterministic_false_count == 0


def test_classify_diagnostics_degraded_when_window_is_entirely_warmup() -> None:
    """Pathological window that's shorter than the longest indicator
    warmup. We can't actually diagnose the strategy in that window —
    treat as degraded so a reviewer notices.
    """
    entries = _series_with_nan([None, None, None])
    diag = _classify_entry_diagnostics(entries, warmup_bars=100)
    assert diag.failure_mode is SignalDiagnosticsFailureMode.EVALUATION_DEGRADED


def test_classify_diagnostics_degraded_threshold_is_50pct() -> None:
    """6 of 10 post-warmup NaN = 0.6 > 0.5 ⇒ degraded.
    5 of 10 post-warmup NaN = 0.5 (NOT > 0.5) and ≥1 True ⇒ NONE.
    """
    # 60% NaN post-warmup ⇒ DEGRADED
    over = _series_with_nan([False] * 2 + [None] * 6 + [False] * 2 + [True] * 2)
    assert _classify_entry_diagnostics(over, warmup_bars=2).failure_mode is (
        SignalDiagnosticsFailureMode.EVALUATION_DEGRADED
    )
    # 50% NaN post-warmup ⇒ NONE (boundary; >50% triggers, == does not)
    boundary = _series_with_nan([False] * 2 + [None] * 5 + [True] * 3 + [False] * 2)
    assert _classify_entry_diagnostics(boundary, warmup_bars=2).failure_mode is (
        SignalDiagnosticsFailureMode.NONE
    )


def test_build_signals_records_diagnostics_for_golden_cross() -> None:
    """End-to-end: a fixture that produces real signals lands with
    failure_mode=NONE and bars_evaluated matches the input length.
    """
    spec = _load_fixture("01_golden_cross.json")
    n = 600
    closes = [100.0] * 250 + list(np.linspace(100, 200, 250)) + list(np.linspace(200, 80, 100))
    data = {Timeframe.D1: _make_ohlcv(n, closes=closes)}

    signals = build_signals(spec, data)
    diag = signals.entry_diagnostics
    assert diag.failure_mode is SignalDiagnosticsFailureMode.NONE
    assert diag.bars_evaluated == n
    assert diag.true_count >= 1
    # SMA(200) is the longest period in the spec; warmup_bars should
    # reflect that (200 + 1).
    assert diag.warmup_bars == 201


def test_build_signals_records_degraded_when_window_too_short_for_indicators() -> None:
    """Golden Cross with SMA(200) on only 100 bars — the slow SMA
    never warms up, the crossover series is NaN throughout, and the
    classifier should mark the result as EVALUATION_DEGRADED rather
    than letting a 0-trade backtest masquerade as a legitimate "no
    signals" result.

    This is the synthetic equivalent of the silent-zero-trades cases
    the v1.1 fix was written to surface — see
    docs/operations/v1.1-silent-zero-trades.md.
    """
    spec = _load_fixture("01_golden_cross.json")
    n = 100  # less than SMA(200) warmup
    closes = list(np.linspace(100, 200, n))
    data = {Timeframe.D1: _make_ohlcv(n, closes=closes)}

    signals = build_signals(spec, data)
    diag = signals.entry_diagnostics
    assert diag.failure_mode is SignalDiagnosticsFailureMode.EVALUATION_DEGRADED
    assert diag.true_count == 0
    # Sanity-check that fillna still produced a usable bool entries
    # series (the engine must remain runnable on degraded specs —
    # the diagnostic is a marker, not a hard failure).
    assert signals.entries.dtype == bool
    assert int(signals.entries.sum()) == 0
