"""3.1 ml_hourly_btc_longflat — cost-gated hourly BTC spot ML strategy.

Long/flat only, 1h closed bars. The core of the strategy is the EV gate:

    EV_bps = (2*p_up - 1) * E[|move|]_bps - round_trip_cost_bps - safety_margin_bps

Enter long iff EV_bps > 0 AND p_up >= p_min. The EV>0 floor is never
relaxed — not to hit a trade-frequency target, not for any reason
(test_ftr_ev_gate proves the floor cannot be configured away).

Models: XGBoost classifier (primary; tree_method='hist', fixed seed,
n_jobs=1 => deterministic) and L2 logistic regression (mandatory baseline).
The XGBoost variant must beat the logistic baseline out-of-sample on NET
metrics to be PASS-eligible; otherwise the simpler model is the candidate.

E[|move|]_bps = k * ATR_H_bps — deterministic, no second ML model in v1;
``k`` and probability calibration (isotonic) are fit on validation slices
only. Test slices are touched once.

Evidence prior (Bysik & Slepaczuk 2026; Sebastiao & Godinho 2021): real but
small predictability; viability is venue-conditional; an honest FAIL or
CONDITIONAL_PASS (binance reference only) is an expected outcome.
"""

from __future__ import annotations

import hashlib
import pickle
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import structlog
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from marketmind_workers.ftr.backtest.costs import cost_breakdown
from marketmind_workers.ftr.data.ohlcv import dtindex
from marketmind_workers.ftr.features.hourly import (
    HourlyFeatureConfig,
    atr_h_bps,
    col,
    compute_hourly_features,
)
from marketmind_workers.ftr.features.shifting import forward_label
from marketmind_workers.ftr.features.splits import Fold
from marketmind_workers.ftr.strategies.specs import MLHourlySpec

logger = structlog.get_logger(__name__)

# Reason-code strings used in the vectorized decision frame (cheap to store
# per-bar; mapped to ReasonCode in DecisionRecord emission).
R_ENTER = "ENTER_EV_POSITIVE"
R_SKIP_EV = "SKIP_EV_NEGATIVE"
R_SKIP_PROB = "SKIP_PROB_BELOW_MIN"
R_SKIP_COST = "SKIP_COST_DOMINATES"
R_EXIT_PROB = "EXIT_PROB_DECAY"
R_EXIT_TRAIL = "EXIT_TRAIL_STOP"
R_EXIT_MAXHOLD = "EXIT_MAX_HOLD"
R_HOLD_POS = "HOLD_IN_POSITION"
R_HOLD_NONE = "HOLD_NO_SIGNAL"


@dataclass(frozen=True)
class FoldModel:
    """Per-fold artifacts: fitted model + calibrator + k, all val-fit only."""

    fold_id: int
    model_family: str
    k_calibration: float
    model_version: str
    val_auc: float
    model_hash: str  # sha256 of the serialized fitted model
    artifact_path: str | None  # where the artifact was persisted (if at all)
    # Test-slice outputs (touched once):
    test_p_up: pd.Series


def build_dataset(
    ohlcv: pd.DataFrame,
    horizon: int,
    *,
    feature_config: HourlyFeatureConfig | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """(features X, binary label y, forward log-return r) aligned, NaN-free.

    Label = sign of forward H-bar log return. Rows with any NaN feature
    (warmup) or NaN label (tail H bars) are dropped, never filled.
    """
    feats = compute_hourly_features(ohlcv, feature_config)
    fwd = forward_label(col(ohlcv, "close"), horizon, kind="log")
    mask = feats.notna().all(axis=1) & fwd.notna()
    x = feats.loc[mask]
    r = fwd.loc[mask]
    y = (r > 0).astype("int64")
    return x, y, r


def _make_model(family: str, seed: int) -> Pipeline | XGBClassifier:
    if family == "xgboost":
        return XGBClassifier(
            n_estimators=300,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=20,
            tree_method="hist",
            random_state=seed,
            n_jobs=1,  # single-threaded => deterministic
            eval_metric="logloss",
        )
    if family == "logistic":
        return Pipeline(
            [
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(C=1.0, penalty="l2", max_iter=2000, random_state=seed)),
            ]
        )
    raise ValueError(f"unknown model family {family!r}")


def _serialize_model(model: Pipeline | XGBClassifier) -> bytes:
    if isinstance(model, XGBClassifier):
        raw = model.get_booster().save_raw(raw_format="json")
        return bytes(raw)
    return pickle.dumps(model)


def fit_fold(
    x: pd.DataFrame,
    y: pd.Series,
    r: pd.Series,
    atr_h: pd.Series,
    fold: Fold,
    *,
    spec: MLHourlySpec,
    model_family: str,
    bar_hours: int = 1,
    persist_dir: Path | None = None,
) -> FoldModel | None:
    """Train on the fold's train slice, calibrate on val, predict test once."""
    idx = dtindex(x)
    bar = timedelta(hours=bar_hours)
    train_m, val_m, test_m = fold.masks(idx, purge_bars=spec.horizon_bars, bar=bar)
    if train_m.sum() < 1000 or val_m.sum() < 100 or test_m.sum() < 100:
        return None

    x_tr, y_tr = x.loc[train_m.to_numpy()], y.loc[train_m.to_numpy()]
    x_val, y_val = x.loc[val_m.to_numpy()], y.loc[val_m.to_numpy()]
    x_te = x.loc[test_m.to_numpy()]
    if y_tr.nunique() < 2 or y_val.nunique() < 2:
        return None

    model = _make_model(model_family, spec.seed)
    model.fit(x_tr.to_numpy(), y_tr.to_numpy())

    raw_val = model.predict_proba(x_val.to_numpy())[:, 1]
    raw_te = model.predict_proba(x_te.to_numpy())[:, 1]

    # Isotonic probability calibration — fit on the validation slice ONLY.
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(raw_val, y_val.to_numpy())
    cal_te = iso.predict(raw_te)
    val_auc = float(roc_auc_score(y_val.to_numpy(), raw_val))

    # k calibration on validation only: realized |move| vs ATR-implied move.
    r_val = r.loc[val_m.to_numpy()]
    atr_val = atr_h.loc[val_m.to_numpy()]
    realized_bps = np.abs(r_val.to_numpy()) * 1e4
    implied_bps = atr_val.to_numpy()
    valid = implied_bps > 0
    k = float(np.median(realized_bps[valid] / implied_bps[valid])) if valid.any() else 1.0

    blob = _serialize_model(model)
    model_hash = hashlib.sha256(blob).hexdigest()[:24]
    artifact_path: str | None = None
    if persist_dir is not None:
        persist_dir.mkdir(parents=True, exist_ok=True)
        out = persist_dir / f"fold{fold.fold_id:03d}-{model_hash}.bin"
        out.write_bytes(blob)
        artifact_path = str(out)

    return FoldModel(
        fold_id=fold.fold_id,
        model_family=model_family,
        k_calibration=k,
        model_version=f"{model_family}-h{spec.horizon_bars}-f{fold.fold_id}-s{spec.seed}",
        val_auc=val_auc,
        model_hash=model_hash,
        artifact_path=artifact_path,
        test_p_up=pd.Series(cal_te, index=x_te.index, name="p_up"),
    )


@dataclass(frozen=True)
class DecisionFrame:
    """Vectorized decision stream for one OOS window: one row per bar."""

    frame: pd.DataFrame  # columns: action, reason, p_up, ev_bps, position


def decide_window(
    ohlcv: pd.DataFrame,
    p_up: pd.Series,
    *,
    spec: MLHourlySpec,
    k_calibration: float | pd.Series,
    cost_multiplier: float = 1.0,
) -> DecisionFrame:
    """Apply the EV gate + exit logic over one window of calibrated p_up.

    ``k_calibration`` may be a scalar or a per-bar Series (per-fold k mapped
    onto each fold's test window — calibrated on validation slices only).
    Sequential by construction (position state, trailing stop, max-hold all
    depend on history). Deterministic: same inputs => same output.
    """
    window = ohlcv.loc[p_up.index]
    atr_h = atr_h_bps(ohlcv, spec.horizon_bars).loc[p_up.index]
    atr_1 = atr_h_bps(ohlcv, 1).loc[p_up.index]  # 1-bar ATR bps for the trail
    close = col(window, "close")
    costs = cost_breakdown(
        spec.venue_profile,
        spec.instrument.symbol,
        multiplier=cost_multiplier,
    )

    e_move_bps = k_calibration * atr_h
    edge_bps = (2.0 * p_up - 1.0) * e_move_bps
    ev_bps = edge_bps - costs.round_trip_bps - spec.safety_margin_bps

    max_hold = spec.max_hold_horizon_multiple * spec.horizon_bars
    exit_p = 0.50 - spec.exit_hysteresis

    n = len(window)
    actions = np.empty(n, dtype=object)
    reasons = np.empty(n, dtype=object)
    position = np.zeros(n, dtype="int64")

    in_pos = False
    entry_ref = 0.0
    highest_close = 0.0
    bars_held = 0

    p_arr = p_up.to_numpy()
    ev_arr = ev_bps.to_numpy()
    edge_arr = edge_bps.to_numpy()
    close_arr = close.to_numpy()
    atr1_arr = (atr_1.to_numpy() * 1e-4) * close_arr  # ATR in price units

    for i in range(n):
        if in_pos:
            bars_held += 1
            highest_close = max(highest_close, close_arr[i])
            trail_level = highest_close - spec.trail_atr_multiple * atr1_arr[i]
            if p_arr[i] < exit_p:
                actions[i], reasons[i], in_pos = "EXIT", R_EXIT_PROB, False
            elif close_arr[i] <= trail_level:
                actions[i], reasons[i], in_pos = "EXIT", R_EXIT_TRAIL, False
            elif bars_held >= max_hold:
                actions[i], reasons[i], in_pos = "EXIT", R_EXIT_MAXHOLD, False
            else:
                actions[i], reasons[i] = "HOLD", R_HOLD_POS
            position[i] = 1 if in_pos else 0
        else:
            if ev_arr[i] > 0.0 and p_arr[i] >= spec.p_min:
                actions[i], reasons[i] = "ENTER_LONG", R_ENTER
                in_pos = True
                entry_ref = close_arr[i]
                highest_close = entry_ref
                bars_held = 0
                position[i] = 1
            else:
                if p_arr[i] < spec.p_min:
                    reasons[i] = R_SKIP_PROB
                elif edge_arr[i] > 0.0:
                    # positive edge but eaten by cost + margin
                    reasons[i] = R_SKIP_COST
                else:
                    reasons[i] = R_SKIP_EV
                actions[i] = "SKIP" if p_arr[i] >= spec.p_min or edge_arr[i] > 0 else "HOLD"
                if actions[i] == "HOLD":
                    reasons[i] = R_HOLD_NONE
                position[i] = 0

    frame = pd.DataFrame(
        {
            "action": actions,
            "reason": reasons,
            "p_up": p_arr,
            "ev_bps": ev_arr,
            "position": position,
        },
        index=p_up.index,
    )
    return DecisionFrame(frame=frame)


def run_walkforward(
    ohlcv: pd.DataFrame,
    folds: list[Fold],
    *,
    spec: MLHourlySpec,
    model_family: str,
    persist_dir: Path | None = None,
) -> tuple[list[FoldModel], pd.Series, pd.Series]:
    """Fit every fold; return (fold models, stitched OOS p_up, per-bar k).

    Stitching: each fold's test window is disjoint by construction (30d test
    rolled by 30d). Where a later fold's window would overlap an earlier one
    we keep the EARLIER fold's predictions (first-touch rule). The per-bar k
    Series carries each fold's validation-calibrated k over its own test
    window so no calibration ever crosses a fold boundary.
    """
    x, y, r = build_dataset(ohlcv, spec.horizon_bars)
    atr_h = atr_h_bps(ohlcv, spec.horizon_bars).loc[x.index]

    fold_models: list[FoldModel] = []
    pieces: list[pd.Series] = []
    k_pieces: list[pd.Series] = []
    seen: pd.DatetimeIndex | None = None
    for fold in folds:
        fm = fit_fold(
            x, y, r, atr_h, fold, spec=spec, model_family=model_family, persist_dir=persist_dir
        )
        if fm is None:
            logger.warning("ftr_ml_fold_skipped", fold=fold.fold_id, family=model_family)
            continue
        chunk = fm.test_p_up
        if seen is not None:
            chunk = chunk.loc[~chunk.index.isin(seen)]
        cidx = dtindex(chunk.to_frame())
        if seen is None:
            seen = cidx
        else:
            joined = seen.append(cidx)
            assert isinstance(joined, pd.DatetimeIndex)
            seen = joined
        fold_models.append(fm)
        pieces.append(chunk)
        k_pieces.append(pd.Series(fm.k_calibration, index=chunk.index))

    if not pieces:
        raise ValueError("no folds produced predictions")
    stitched = pd.concat(pieces).sort_index()
    k_series = pd.concat(k_pieces).sort_index()
    return fold_models, stitched, k_series
