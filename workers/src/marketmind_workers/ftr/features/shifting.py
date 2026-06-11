"""THE anti-lookahead module — all FTR temporal discipline lives here.

Global rule (mandate Stage 2): every feature at bar ``t`` may use only data
with timestamp <= close of bar ``t``. Labels for horizon ``H`` use
``close[t+H]/close[t] - 1`` — strictly future data, never visible to
features. Enforced by construction here and by test
(test_ftr_no_lookahead_features truncation test).

Every feature pipeline must build features from *past-or-current* values via
these helpers rather than calling ``shift`` ad hoc. The convention:

- ``lag(s, k)``      — value k bars BEFORE t (k >= 1) or at t (k == 0)
- ``rolling_past(s, w, fn)`` — rolling window ENDING at t (inclusive);
  legitimate because bar t's close is known at bar t's close
- ``forward_label(close, h)`` — log or simple return from t to t+h; the ONLY
  function in FTR allowed to look forward, used exclusively for labels
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def lag(s: pd.Series, k: int) -> pd.Series:
    """Value k bars in the past. k must be >= 0; k=0 is the current bar."""
    if k < 0:
        raise ValueError(f"lag must be >= 0, got {k} (forward shifts are labels, not features)")
    out = s.shift(k)
    assert isinstance(out, pd.Series)
    return out


def lagged_log_return(close: pd.Series, k: int) -> pd.Series:
    """Log return over the k bars ENDING at t: ln(close[t] / close[t-k])."""
    if k < 1:
        raise ValueError(f"lagged_log_return needs k >= 1, got {k}")
    arr = np.log(close.to_numpy(dtype="float64"))
    return pd.Series(arr, index=close.index).diff(k)


def forward_label(close: pd.Series, horizon: int, *, kind: str = "log") -> pd.Series:
    """Forward H-bar return label: close[t+H]/close[t] - 1 (or log).

    THE ONLY forward-looking function in FTR. The trailing ``horizon`` bars
    are NaN — callers must drop them, never fill them.
    """
    if horizon < 1:
        raise ValueError(f"label horizon must be >= 1, got {horizon}")
    future = close.shift(-horizon)
    if kind == "log":
        return pd.Series(
            np.log(future.to_numpy(dtype="float64") / close.to_numpy(dtype="float64")),
            index=close.index,
        )
    if kind == "simple":
        return future / close - 1.0
    raise ValueError(f"unknown label kind {kind!r}")


def assert_no_future_index(features: pd.DataFrame, labels: pd.Series) -> None:
    """Sanity guard wired into dataset assembly: identical index, no reorder."""
    if not features.index.equals(labels.index):
        raise ValueError("features and labels must share an identical index")
    if not features.index.is_monotonic_increasing:
        raise ValueError("dataset index must be monotonically increasing")
