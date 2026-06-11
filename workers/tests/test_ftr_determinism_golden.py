"""Determinism: fixed fixture + fixed seed => byte-identical decision log
and equity-curve hash, pinned against a golden file.

The golden hash was produced by running this test's pipeline once on the
build machine (xgboost hist, n_jobs=1, seed 1729). If a library upgrade
legitimately changes it, regenerate with FTR_UPDATE_GOLDEN=1 and record the
library versions in the commit message.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from marketmind_workers.ftr.backtest.costs import cost_breakdown
from marketmind_workers.ftr.backtest.vector_engine import run_vector_backtest
from marketmind_workers.ftr.features.splits import make_walkforward_folds
from marketmind_workers.ftr.strategies.ml_hourly import (
    build_dataset,
    decide_window,
    fit_fold,
)
from marketmind_workers.ftr.strategies.specs import MLHourlySpec

from .ftr_helpers import synthetic_ohlcv

GOLDEN_PATH = Path(__file__).parent / "goldens" / "ftr_determinism.sha256"


def _pipeline_hash() -> str:
    df = synthetic_ohlcv(n_bars=24 * 500, seed=99, drift=0.0001, vol=0.008)
    spec = MLHourlySpec.model_validate(
        {
            "kind": "ml_hourly_longflat",
            "strategy_id": "golden",
            "venue_profile": "binance_spot_reference",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
            "horizon_bars": 12,
            "seed": 1729,
        }
    )
    from marketmind_workers.ftr.features.hourly import atr_h_bps

    x, y, r = build_dataset(df, spec.horizon_bars)
    folds = make_walkforward_folds(df, min_folds=2)
    fm = fit_fold(
        x, y, r, atr_h_bps(df, 12).loc[x.index], folds[0], spec=spec, model_family="xgboost"
    )
    assert fm is not None
    dec = decide_window(df, fm.test_p_up, spec=spec, k_calibration=fm.k_calibration)
    result = run_vector_backtest(
        df.loc[dec.frame.index],
        dec.frame["position"],
        cost_breakdown("binance_spot_reference", "BTC/USDT"),
    )
    h = hashlib.sha256()
    h.update(dec.frame.to_csv().encode())
    h.update(result.equity.to_csv().encode())
    h.update(fm.model_hash.encode())
    return h.hexdigest()


def test_same_inputs_same_bytes() -> None:
    """Two runs in the same process produce byte-identical outputs."""
    assert _pipeline_hash() == _pipeline_hash()


def test_golden_hash_pinned() -> None:
    digest = _pipeline_hash()
    if os.getenv("FTR_UPDATE_GOLDEN") == "1" or not GOLDEN_PATH.exists():
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(digest + "\n")
    pinned = GOLDEN_PATH.read_text().strip()
    assert digest == pinned, (
        f"determinism drift: pipeline hash {digest} != golden {pinned}. If a "
        "deliberate library upgrade caused this, regenerate with FTR_UPDATE_GOLDEN=1."
    )
