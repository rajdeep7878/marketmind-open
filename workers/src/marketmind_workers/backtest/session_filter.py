"""Phase C C.5 — session-aware DataFrame filtering for non-24/7 markets.

The C.4.1 schema introduced `Instrument.session_hours: SessionHours | None`.
When `session_hours.weekend_closed = True` (the FX / metals / equity
default), the backtest engine must drop rows where the timestamp falls
on Saturday or Sunday — Oanda may still return weekend bars in some
edge cases, and synthetic test fixtures (and the engine's own next-bar-
open `.shift(-1)` semantics) get poisoned if weekends are treated as
trade-able.

Crypto bit-identity is preserved by the gated dispatch: when
`spec.instrument.session_hours is None` (every pre-C.5 spec and every
crypto_spot spec since C.1.1), this helper is a no-op pass-through.

Minimum-path scope per the previous session's analysis: no
`pandas_market_calendars` library, no per-venue holiday tables, no DST
helper. Holiday handling lands in C.4-full alongside equities in C.9.
This module ships only the structural weekend-skip (`weekday >= 5`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pandas as pd

if TYPE_CHECKING:
    from marketmind_shared.schemas.strategy_spec import StrategySpec, Timeframe


def drop_weekends_if_session_closed(
    df: pd.DataFrame,
    spec: StrategySpec,
) -> pd.DataFrame:
    """Drop Saturday + Sunday rows from `df` when the spec's instrument
    declares a `weekend_closed=True` SessionHours.

    Crypto specs (and every pre-C.5 spec) have `session_hours=None` and
    this function returns `df` unchanged — same object, no copy. The
    crypto_spot path is therefore observationally identical to pre-C.5.

    For specs with `session_hours.weekend_closed=True`, returns a NEW
    DataFrame with Sat / Sun rows removed (via `.copy()` so downstream
    mutations don't surprise the caller). Index must be a tz-aware
    `DatetimeIndex`; the helper trusts the engine's existing
    validation upstream (engine.py / iterative.py both already assert
    DatetimeIndex).

    Implementation: pandas' `DatetimeIndex.weekday` returns int 0-6 with
    Monday=0, Sunday=6. Saturday=5, Sunday=6 — both >= 5. Filter
    `df[df.index.weekday < 5]` is equivalent to `df[df.index.weekday <= 4]`
    and matches the design doc §5.3 "drop weekend bars in backtest" rule.
    """
    sh = spec.instrument.session_hours
    if sh is None or not sh.weekend_closed:
        return df
    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError(
            "drop_weekends_if_session_closed: expected DataFrame with "
            f"DatetimeIndex, got {type(df.index).__name__}",
        )
    # `.weekday` is a numpy int array — Mon=0..Sun=6. Weekend rows fail
    # the `< 5` test (Sat=5, Sun=6) and are filtered out. The casts
    # narrow pandas-stubs' DatetimeIndex.weekday + boolean-index types
    # for pyright; both are runtime-safe by the isinstance guard above.
    weekday = df.index.weekday  # type: ignore[attr-defined]
    return cast("pd.DataFrame", df[weekday < 5]).copy()


def drop_weekends_in_data_dict(
    data: dict[Timeframe, pd.DataFrame],
    spec: StrategySpec,
) -> dict[Timeframe, pd.DataFrame]:
    """Apply `drop_weekends_if_session_closed` to every timeframe in a
    backtest's data dict.

    The engine's `_load_required_data` returns a `dict[Timeframe,
    pd.DataFrame]` covering the primary plus any filter timeframe.
    Both must be dropped consistently — otherwise the filter-tf signal
    series and the primary-tf signal series fall out of index alignment
    on weekend boundaries, producing silent join-on-weekend NaNs that
    cascade into the engine's downstream `.shift(-1)` arithmetic.

    For crypto specs (the no-op path), this returns the SAME dict
    reference with no copies. For weekend-closed specs, returns a NEW
    dict with each value replaced by its weekend-dropped copy.
    """
    if spec.instrument.session_hours is None or not spec.instrument.session_hours.weekend_closed:
        return data
    return {tf: drop_weekends_if_session_closed(df, spec) for tf, df in data.items()}


__all__ = [
    "drop_weekends_if_session_closed",
    "drop_weekends_in_data_dict",
]
