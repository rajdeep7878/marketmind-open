"""Labels use strictly future data; purge/embargo boundaries respected."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from marketmind_workers.ftr.data.ohlcv import dtindex
from marketmind_workers.ftr.features.shifting import forward_label
from marketmind_workers.ftr.features.splits import make_walkforward_folds

from .ftr_helpers import synthetic_ohlcv


def test_forward_label_strictly_future() -> None:
    df = synthetic_ohlcv(n_bars=300, seed=5)
    h = 12
    lab = forward_label(df["close"], h)
    # tail h bars must be NaN — no future data exists for them
    assert lab.tail(h).isna().all()
    # spot-check the definition: ln(close[t+h]/close[t])
    t = 100
    expected = np.log(df["close"].iloc[t + h] / df["close"].iloc[t])
    assert abs(lab.iloc[t] - expected) < 1e-12


def test_purge_and_embargo_boundaries() -> None:
    df = synthetic_ohlcv(n_bars=24 * 800, seed=9)  # ~800 days hourly
    folds = make_walkforward_folds(df, min_folds=12)
    h = 12
    bar = timedelta(hours=1)
    idx = dtindex(df)
    for fold in folds[:3]:
        train_m, val_m, test_m = fold.masks(idx, purge_bars=h, bar=bar)
        train_idx = idx[train_m.to_numpy()]
        val_idx = idx[val_m.to_numpy()]
        test_idx = idx[test_m.to_numpy()]
        # purge: last training bar's label window [t, t+h] must end before
        # the validation slice starts
        assert train_idx.max() + h * bar <= val_idx.min()
        # embargo: >= 24h between validation end and test start
        assert test_idx.min() - val_idx.max() >= timedelta(hours=24)
        # no overlap anywhere
        assert len(set(train_idx) & set(val_idx)) == 0
        assert len(set(val_idx) & set(test_idx)) == 0


def test_min_folds_enforced() -> None:
    df = synthetic_ohlcv(n_bars=24 * 30)  # only ~30 days
    with pytest.raises(ValueError, match="need >= 12"):
        make_walkforward_folds(df)
