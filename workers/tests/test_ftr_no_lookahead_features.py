"""Anti-lookahead: features at t computed on data truncated at t equal
features at t from the full series, across sampled t (mandate Stage 7)."""

from __future__ import annotations

import numpy as np
import pytest
from marketmind_workers.ftr.features.hourly import compute_hourly_features
from marketmind_workers.ftr.features.shifting import lag, lagged_log_return

from .ftr_helpers import synthetic_ohlcv


def test_truncation_invariance() -> None:
    df = synthetic_ohlcv(n_bars=1500, seed=11)
    full = compute_hourly_features(df)
    rng = np.random.default_rng(3)
    for t in sorted(rng.integers(600, 1499, size=12)):
        truncated = compute_hourly_features(df.iloc[: t + 1])
        row_full = full.iloc[t]
        row_trunc = truncated.iloc[-1]
        np.testing.assert_allclose(
            row_trunc.to_numpy(dtype="float64"),
            row_full.to_numpy(dtype="float64"),
            rtol=1e-9,
            atol=1e-12,
            err_msg=f"feature lookahead at bar {t}",
        )


def test_negative_lag_rejected() -> None:
    df = synthetic_ohlcv(n_bars=100)
    with pytest.raises(ValueError, match="forward shifts are labels"):
        lag(df["close"], -1)
    with pytest.raises(ValueError, match="k >= 1"):
        lagged_log_return(df["close"], 0)
