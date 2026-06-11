"""Purged walk-forward splits with embargo (mandate Stage 2).

Fold anatomy (ML walk-forward, §3.1):

    [ train 365d ][ purge H bars ][ calibration/validation 30d ][ embargo 24h ][ test 30d ]

- purge = label horizon H bars: the last H training labels overlap the
  validation window's price path, so those rows are dropped from train.
- embargo = 24h minimum between the end of the calibration slice and the
  start of test, so serially-correlated features cannot leak across.
- the calibration/validation slice sits strictly between train and test;
  thresholds and k-calibration may use it; test slices are touched once.

Rolled forward by ``step`` (30d) until the data runs out; >= min_folds folds
required or the caller must widen the data range.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from marketmind_workers.ftr.data.ohlcv import dtindex


@dataclass(frozen=True)
class Fold:
    fold_id: int
    train_start: datetime
    train_end: datetime  # exclusive, BEFORE purge
    val_start: datetime
    val_end: datetime  # exclusive
    test_start: datetime
    test_end: datetime  # exclusive

    def masks(
        self, index: pd.DatetimeIndex, *, purge_bars: int, bar: timedelta
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Boolean (train, val, test) masks over `index` with purge applied.

        The purge removes the last ``purge_bars`` rows of train so that no
        training label's forward window reaches into validation.
        """
        purge_cut = self.train_end - purge_bars * bar
        train = pd.Series((index >= self.train_start) & (index < purge_cut), index=index)
        val = pd.Series((index >= self.val_start) & (index < self.val_end), index=index)
        test = pd.Series((index >= self.test_start) & (index < self.test_end), index=index)
        return train, val, test


def make_walkforward_folds(
    df: pd.DataFrame,
    *,
    train_days: int = 365,
    val_days: int = 30,
    test_days: int = 30,
    step_days: int = 30,
    embargo_hours: int = 24,
    min_folds: int = 12,
) -> list[Fold]:
    """Rolling purged walk-forward folds over a frame's full time span."""
    idx = dtindex(df)
    if len(idx) == 0:
        raise ValueError("empty frame")
    first, last = idx[0], idx[-1]
    assert isinstance(first, pd.Timestamp) and isinstance(last, pd.Timestamp)
    start = first.to_pydatetime()
    end = last.to_pydatetime()

    folds: list[Fold] = []
    fold_id = 0
    cursor = start
    while True:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        val_start = train_end
        val_end = val_start + timedelta(days=val_days)
        test_start = val_end + timedelta(hours=embargo_hours)
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(
            Fold(
                fold_id=fold_id,
                train_start=train_start,
                train_end=train_end,
                val_start=val_start,
                val_end=val_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        fold_id += 1
        cursor = cursor + timedelta(days=step_days)

    if len(folds) < min_folds:
        raise ValueError(
            f"only {len(folds)} folds fit the data span "
            f"[{start.isoformat()}, {end.isoformat()}]; need >= {min_folds}"
        )
    return folds
