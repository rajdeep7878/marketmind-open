"""Phase C C.5 — unit tests for the session-aware DataFrame filter.

Two helpers under test:
  - drop_weekends_if_session_closed(df, spec) — single DataFrame
  - drop_weekends_in_data_dict(data, spec) — full {Timeframe: df}

Test focus:
  1. The load-bearing crypto bit-identity: spec.instrument.session_hours
     = None must return the SAME df object (no copy) — bit-identical
     for every pre-C.5 spec.
  2. weekend_closed=True drops Sat + Sun and ONLY Sat + Sun.
  3. weekend_closed=False (the 24/7 identity case) acts as no-op.
  4. Multi-timeframe dict drops all frames consistently.
  5. Non-DatetimeIndex DataFrames raise (defensive guard).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import numpy as np
import pandas as pd
import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import SessionHours, Timeframe
from marketmind_workers.backtest.session_filter import (
    drop_weekends_if_session_closed,
    drop_weekends_in_data_dict,
)

# ---- fixtures -------------------------------------------------------------


def _hourly_df(start: str, n_hours: int) -> pd.DataFrame:
    """Build an N-hour OHLCV DataFrame with a UTC DatetimeIndex starting
    at `start` (any pandas-parseable string).
    """
    idx = pd.date_range(start=start, periods=n_hours, freq="1h", tz=UTC)
    return pd.DataFrame(
        {
            "open": np.arange(n_hours, dtype=float),
            "high": np.arange(n_hours, dtype=float) + 0.1,
            "low": np.arange(n_hours, dtype=float) - 0.1,
            "close": np.arange(n_hours, dtype=float) + 0.05,
            "volume": np.full(n_hours, 1000.0),
        },
        index=idx,
    )


def _spec_with_session_hours(sh: SessionHours | None) -> Any:
    """Build a minimal valid v1 spec; optionally override session_hours
    on the Instrument. None reproduces the pre-C.1.1 crypto default.
    """
    instrument: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "quote_currency": "USDT",
    }
    if sh is not None:
        instrument["asset_class"] = "fx_spot"
        instrument["session_hours"] = sh.model_dump()
    spec, _warnings = validate_spec(
        {
            "schema_version": "1.0",
            "name": "test",
            "instrument": instrument,
            "primary_timeframe": "1h",
            "direction": "long",
            "entry": {
                "condition": {
                    "type": "compare",
                    "left": {"kind": "price", "field": "close"},
                    "op": ">",
                    "right": {"kind": "constant", "value": 100.0},
                },
                "order_type": "market",
            },
            "exit": {
                "exits": [
                    {"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}},
                ],
            },
        },
    )
    return spec


# ---- crypto bit-identity (no SessionHours) — the load-bearing guard ------


def test_crypto_spec_returns_same_object_no_copy() -> None:
    """THE load-bearing C.5 regression: a spec with
    session_hours=None (every crypto_spot strategy + every pre-C.1.1
    spec) must return the SAME DataFrame reference, not a copy.

    Returning the same object proves no allocation or row mutation
    happens on the crypto path — the bit-identical guarantee.
    """
    df = _hourly_df("2025-01-01 00:00", 48)  # 2 days
    spec = _spec_with_session_hours(None)
    out = drop_weekends_if_session_closed(df, spec)
    assert out is df, "crypto path must return same object (no copy)"


def test_weekend_closed_false_returns_same_object_no_copy() -> None:
    """An explicit 24/7 SessionHours (weekend_closed=False) is also a
    no-op — same object, no copy, bit-identical with the None path.
    Covers the crypto edge case where a future spec carries an
    explicit 24/7 SessionHours for symmetry.
    """
    df = _hourly_df("2025-01-01 00:00", 48)
    sh = SessionHours(calendar="24/7", open_utc="00:00", close_utc="00:00",
                      weekend_closed=False)
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_if_session_closed(df, spec)
    assert out is df, "weekend_closed=False must return same object"


# ---- weekend-closed dispatch (the new C.5 behavior) ----------------------


def test_weekend_closed_true_drops_sat_and_sun() -> None:
    """A 1-week DataFrame (Mon 2025-01-06 00:00 through Sun 23:00):
    168 rows total; weekend = 48 rows (Sat + Sun); should leave 120
    weekday rows.
    """
    df = _hourly_df("2025-01-06 00:00", 168)  # Monday 06 Jan 2025 start
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00",
                      weekend_closed=True)
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_if_session_closed(df, spec)
    # 5 weekdays × 24h = 120 rows.
    assert len(out) == 120, f"expected 120 weekday rows, got {len(out)}"
    # Every remaining row must be a weekday.
    assert (out.index.weekday < 5).all(), "non-weekday rows remain after drop"  # type: ignore[union-attr]
    # Sanity: dropping was a copy (mutation safety).
    assert out is not df


def test_weekend_closed_true_preserves_weekday_data() -> None:
    """Drop must preserve the original values for weekday rows — only
    weekend rows go.
    """
    df = _hourly_df("2025-01-06 00:00", 168)
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_if_session_closed(df, spec)
    # Mon 00:00 should be the same row (close=0.05 from the fixture).
    mon_00 = pd.Timestamp("2025-01-06 00:00", tz="UTC")
    assert out.loc[mon_00, "close"] == 0.05  # type: ignore[index]
    # The hour BEFORE Sat (Fri 23:00, idx position 167 - 24*2 - 1 = 119)
    # should match df's value at that timestamp.
    fri_23 = pd.Timestamp("2025-01-10 23:00", tz="UTC")
    assert out.loc[fri_23, "close"] == df.loc[fri_23, "close"]  # type: ignore[index]


def test_weekend_closed_true_drops_only_sat_sun() -> None:
    """All 5 weekdays + both weekend days present in input. Exactly 2 of
    7 weekdays should be dropped (Sat=5, Sun=6 per pandas Mon=0 convention).
    """
    # 7 daily bars starting Monday — one of each weekday.
    idx = pd.date_range(start="2025-01-06 12:00", periods=7, freq="1D", tz=UTC)
    df = pd.DataFrame({"v": np.arange(7, dtype=float)}, index=idx)
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_if_session_closed(df, spec)
    assert len(out) == 5
    assert set(out.index.weekday) == {0, 1, 2, 3, 4}  # type: ignore[union-attr,arg-type]


# ---- multi-timeframe data dict ------------------------------------------


def test_data_dict_crypto_returns_same_dict_reference() -> None:
    """Crypto bit-identity for the data-dict path: same dict reference,
    no per-tf copying.
    """
    data: dict[Timeframe, pd.DataFrame] = {
        Timeframe.H1: _hourly_df("2025-01-01 00:00", 48),
        Timeframe.D1: _hourly_df("2025-01-01 00:00", 7),
    }
    spec = _spec_with_session_hours(None)
    out = drop_weekends_in_data_dict(data, spec)
    assert out is data


def test_data_dict_fx_drops_all_timeframes() -> None:
    """When a spec carries weekend_closed=True, every timeframe in the
    data dict gets weekend-dropped — otherwise filter-tf vs primary-tf
    index alignment breaks at the weekend boundary.
    """
    data: dict[Timeframe, pd.DataFrame] = {
        Timeframe.H1: _hourly_df("2025-01-06 00:00", 168),  # 1 week 1h
        Timeframe.H4: _hourly_df("2025-01-06 00:00", 42),   # 1 week 4h
    }
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_in_data_dict(data, spec)
    # New dict (not the same reference).
    assert out is not data
    # Both frames had weekends dropped.
    for tf, dropped_df in out.items():
        assert (dropped_df.index.weekday < 5).all(), (  # type: ignore[union-attr]
            f"timeframe {tf.value} still has weekend rows after drop"
        )


# ---- defensive guard ------------------------------------------------------


def test_non_datetime_index_raises() -> None:
    """The engine's existing validation guarantees DatetimeIndex, but
    a defensive guard makes the helper safe for direct use in tests.
    """
    df = pd.DataFrame({"v": [1, 2, 3]}, index=pd.RangeIndex(3))  # RangeIndex
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    spec = _spec_with_session_hours(sh)
    with pytest.raises(TypeError, match=r"DatetimeIndex"):
        drop_weekends_if_session_closed(df, spec)


def test_non_datetime_index_no_op_path_does_not_raise() -> None:
    """The defensive guard only fires when the dispatch is active. With
    session_hours=None (crypto), the function is a same-object passthrough
    that doesn't inspect the index — so a non-DatetimeIndex df is fine.
    """
    df = pd.DataFrame({"v": [1, 2, 3]}, index=pd.RangeIndex(3))
    spec = _spec_with_session_hours(None)
    out = drop_weekends_if_session_closed(df, spec)
    assert out is df  # no inspection, no raise


# ---- realistic FX-week example -------------------------------------------


def test_realistic_fx_week_drops_correctly() -> None:
    """A 1-year synthetic 1H DataFrame at FX cadence (24/5): 52 weeks
    × 5 weekdays × 24h = ~6240 weekday rows; weekend = ~2496 rows.
    Note: the dataset starts on a Wednesday (2025-01-01) so the first
    week is partial. We test the structural property — only weekday
    rows survive — not an exact count.
    """
    df = _hourly_df("2025-01-01 00:00", 365 * 24)  # full year, hourly
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_if_session_closed(df, spec)
    # Every surviving row is a weekday.
    assert (out.index.weekday < 5).all()  # type: ignore[union-attr]
    # Sanity: between 5/7 and 6/7 of the input (52 full weeks × 5/7 +
    # 1 day extra). 5/7 of 8760 ≈ 6257; 6/7 ≈ 7508.
    assert 6000 < len(out) < 7000


# ---- ascending-timestamp property ---------------------------------------


def test_drop_preserves_index_order() -> None:
    """Filtering preserves the chronological order (pandas drops by
    boolean mask in-place; no re-sort needed)."""
    df = _hourly_df("2025-01-06 00:00", 168)
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    spec = _spec_with_session_hours(sh)
    out = drop_weekends_if_session_closed(df, spec)
    timestamps = list(out.index)
    assert timestamps == sorted(timestamps)
