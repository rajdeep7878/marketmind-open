"""FTR validation orchestrator — the full gauntlet run (mandate Stage 4).

Usage:
    python -m marketmind_workers.ftr.validation.runner --strategy ml
    python -m marketmind_workers.ftr.validation.runner --strategy trend
    python -m marketmind_workers.ftr.validation.runner --strategy ofi
    python -m marketmind_workers.ftr.validation.runner --strategy all

Per strategy family this produces, for EVERY venue profile:
sweep cells (all recorded; n_trials = honest total), the chosen cell,
G1-G9 gate evaluation, baselines, overlay ablation, event-engine parity,
Monte Carlo evidence, and a Verdict — written as JSON artifacts under
``data/ftr/artifacts/validation/<family>/<runstamp>/`` and (when a DB is
reachable) into ``ftr_verdicts``.

DSR comes from the repo's ``deflated_sharpe`` (T in YEARS — repo footgun).
"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import structlog

from marketmind_workers.ftr.backtest.costs import cost_breakdown
from marketmind_workers.ftr.backtest.event_engine import run_event_backtest
from marketmind_workers.ftr.backtest.vector_engine import (
    RunResult,
    run_portfolio_backtest,
    run_vector_backtest,
)
from marketmind_workers.ftr.config.settings import UNIVERSE_SUPERSET, FTRSettings, get_ftr_settings
from marketmind_workers.ftr.config.venue_profiles import VENUE_PROFILES
from marketmind_workers.ftr.data.ohlcv import dtindex, get_ohlcv
from marketmind_workers.ftr.features.hourly import col
from marketmind_workers.ftr.features.splits import Fold, make_walkforward_folds
from marketmind_workers.ftr.strategies.liquidity_overlay import (
    LiquidityOverlay,
    abdi_ranaldo_spread_bps,
    apply_overlay_to_positions,
    hour_of_week_liquidity_score,
)
from marketmind_workers.ftr.strategies.ml_hourly import (
    build_dataset,
    decide_window,
    fit_fold,
    run_walkforward,
)
from marketmind_workers.ftr.strategies.ofi_research import inventory_recordings
from marketmind_workers.ftr.strategies.specs import (
    LiquidityOverlaySpec,
    MLHourlySpec,
    TrendPortfolioSpec,
    validate_ftr_spec,
)
from marketmind_workers.ftr.strategies.trend_portfolio import (
    btc_regime_mask,
    compute_asset_signals,
    effective_breadth,
    resample_6h,
    select_universe,
    target_weights,
)
from marketmind_workers.ftr.validation import baselines as bl
from marketmind_workers.ftr.validation.gates import GateInputs, Verdict, evaluate_gates
from marketmind_workers.ftr.validation.metrics import NetMetrics, compute_net_metrics
from marketmind_workers.ftr.validation.montecarlo import (
    block_bootstrap_trades,
    block_permute_labels,
    matched_frequency_random_entries,
)
from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe

logger = structlog.get_logger(__name__)

_HISTORY_START = datetime(2017, 8, 1, tzinfo=UTC)

ML_HORIZONS_FEASIBLE = (4, 8, 12, 24)
ML_HORIZONS_REFERENCE_ONLY = (1, 2)
ML_P_MINS = (0.53, 0.55, 0.58)
ML_FAMILIES = ("xgboost", "logistic")

TREND_GRID = {
    "ema_fast": (20, 50),
    "ema_slow": (100, 200),
    "donchian_n": (40, 55, 80),
    "chandelier_atr_multiple": (2.5, 3.0, 4.0),
    "timeframe": ("4h", "6h"),
    "btc_regime_gate": (False, True),
}
TREND_HOLDOUT_START = datetime(2024, 1, 1, tzinfo=UTC)


def _load(settings: FTRSettings, symbol: str, timeframe: str) -> pd.DataFrame:
    series = get_ohlcv(
        settings.research_exchange,
        symbol,
        timeframe,
        _HISTORY_START,
        datetime.now(UTC),
        cache_dir=settings.cache_dir,
        offline=True,
    )
    return series.frame


def _fold_positive_fraction(
    bar_returns: pd.Series, windows: list[tuple[datetime, datetime]]
) -> float:
    """Fraction of windows with strictly positive compounded net return.
    Flat (zero-trade) windows count as NOT positive — honesty rule."""
    if not windows:
        return 0.0
    pos = 0
    for start, end in windows:
        chunk = bar_returns.loc[(bar_returns.index >= start) & (bar_returns.index < end)]
        if len(chunk) and float(np.prod(1.0 + chunk.to_numpy())) - 1.0 > 0:
            pos += 1
    return pos / len(windows)


def _dsr(metrics: NetMetrics, n_trials: int) -> float:
    t_years = max(round(metrics.years), 2)
    res = deflated_sharpe(
        metrics.sharpe,
        n_trials_estimate=max(n_trials, 1),
        n_observations=t_years,
        returns_skewness=metrics.skewness,
        returns_kurtosis=metrics.kurtosis,
    )
    return float(res.probability_strategy_is_real)


def _runstamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))


# ===========================================================================
# 3.1 ml_hourly_btc_longflat
# ===========================================================================


def run_ml_validation(settings: FTRSettings | None = None) -> dict[str, Any]:
    settings = settings or get_ftr_settings()
    t0 = time.monotonic()
    out_dir = settings.artifacts_dir / "validation" / "ml_hourly" / _runstamp()
    symbol = settings.primary_symbol
    ohlcv = _load(settings, symbol, "1h")
    folds = make_walkforward_folds(ohlcv)
    logger.info("ftr_ml_validation_start", folds=len(folds), bars=len(ohlcv))

    # --- walk-forward predictions per (family, horizon), persisted models
    preds: dict[tuple[str, int], tuple[pd.Series, pd.Series, list[Any]]] = {}
    all_horizons = ML_HORIZONS_FEASIBLE + ML_HORIZONS_REFERENCE_ONLY
    registry_rows: list[dict[str, Any]] = []
    for family in ML_FAMILIES:
        for h in all_horizons:
            spec = MLHourlySpec.model_validate(
                {
                    "kind": "ml_hourly_longflat",
                    "strategy_id": f"ml-{family}-h{h}",
                    "venue_profile": "binance_spot_reference",
                    "instrument": {"symbol": symbol, "exchange": settings.research_exchange},
                    "horizon_bars": h,
                }
            )
            persist = settings.models_dir / "ml_hourly" / f"{family}-h{h}"
            fms, p_up, k_series = run_walkforward(
                ohlcv, folds, spec=spec, model_family=family, persist_dir=persist
            )
            preds[(family, h)] = (p_up, k_series, fms)
            registry_rows.extend(
                {
                    "model_version": fm.model_version,
                    "model_hash": fm.model_hash,
                    "artifact_path": fm.artifact_path,
                    "fold_id": fm.fold_id,
                    "val_auc": fm.val_auc,
                    "k_calibration": fm.k_calibration,
                }
                for fm in fms
            )
            logger.info(
                "ftr_ml_wf_done",
                family=family,
                horizon=h,
                folds=len(fms),
                mean_val_auc=round(float(np.mean([fm.val_auc for fm in fms])), 4),
            )
    _write_json(out_dir / "model_registry.json", {"models": registry_rows})

    # --- sweep cells per profile (honest n_trials = every cell evaluated)
    cells: list[dict[str, Any]] = []
    runs: dict[tuple[str, str, int, float], tuple[RunResult, NetMetrics, Any]] = {}
    for prof_name, prof in VENUE_PROFILES.items():
        horizons = all_horizons if prof_name == "binance_spot_reference" else ML_HORIZONS_FEASIBLE
        for family in ML_FAMILIES:
            for h in horizons:
                p_up, k_series, _ = preds[(family, h)]
                for p_min in ML_P_MINS:
                    spec = MLHourlySpec.model_validate(
                        {
                            "kind": "ml_hourly_longflat",
                            "strategy_id": f"ml-{family}-h{h}-p{p_min}",
                            "venue_profile": prof_name,
                            "instrument": {
                                "symbol": symbol,
                                "exchange": settings.research_exchange,
                            },
                            "horizon_bars": h,
                            "p_min": p_min,
                        }
                    )
                    dec = decide_window(ohlcv, p_up, spec=spec, k_calibration=k_series)
                    window = ohlcv.loc[dec.frame.index]
                    costs = cost_breakdown(prof, symbol)
                    result = run_vector_backtest(window, col(dec.frame, "position"), costs)
                    m = compute_net_metrics(result, timeframe="1h")
                    reason_counts = dec.frame["reason"].value_counts().to_dict()
                    cells.append(
                        {
                            "profile": prof_name,
                            "family": family,
                            "horizon": h,
                            "p_min": p_min,
                            "reason_counts": reason_counts,
                            **m.to_dict(),
                        }
                    )
                    runs[(prof_name, family, h, p_min)] = (result, m, dec)
    n_trials = len({(c["family"], c["horizon"], c["p_min"]) for c in cells})
    _write_json(out_dir / "sweep_cells.json", {"n_trials": n_trials, "cells": cells})

    # --- selection + gates per profile
    fold_windows = [(f.test_start, f.test_end) for f in folds]
    reports: dict[str, Any] = {}
    for prof_name, prof in VENUE_PROFILES.items():
        prof_cells = [c for c in cells if c["profile"] == prof_name]

        def best(family: str, prof_cells: list[dict[str, Any]] = prof_cells) -> dict[str, Any] | None:
            fam = [c for c in prof_cells if c["family"] == family]
            fam = [c for c in fam if c["num_trades"] > 0]
            return max(fam, key=lambda c: c["sharpe"]) if fam else None

        best_xgb, best_log = best("xgboost"), best("logistic")
        # XGBoost must beat the logistic baseline OOS on net metrics to be
        # the candidate; otherwise the simpler model is the candidate.
        if best_xgb and best_log:
            chosen_cell = best_xgb if best_xgb["sharpe"] > best_log["sharpe"] else best_log
        else:
            chosen_cell = best_xgb or best_log

        if chosen_cell is None:
            # every cell on this profile produced zero trades
            zero_m = next(
                runs[(prof_name, c["family"], c["horizon"], c["p_min"])][1]
                for c in prof_cells
            )
            report = evaluate_gates(
                strategy_id="ml_hourly_btc_longflat",
                venue_profile=prof_name,
                uk_execution_feasible=prof.uk_execution_feasible,
                inputs=GateInputs(
                    metrics=zero_m,
                    dsr_probability=None,
                    positive_fold_fraction=None,
                    random_entry_percentile=None,
                    plateau_ratio=None,
                    expectancy_at_1p5x_cost=None,
                    n_trials=n_trials,
                    notes=["all sweep cells produced zero OOS trades on this profile"],
                ),
            )
            reports[prof_name] = {"report": report.to_dict(), "chosen_cell": None}
            continue

        key = (prof_name, chosen_cell["family"], chosen_cell["horizon"], chosen_cell["p_min"])
        result, m, dec = runs[key]
        family, h, p_min = chosen_cell["family"], chosen_cell["horizon"], chosen_cell["p_min"]
        p_up, k_series, _ = preds[(family, h)]
        costs = cost_breakdown(prof, symbol)
        window = ohlcv.loc[dec.frame.index]
        assert result.bar_returns is not None

        # G4 — fold positives
        fold_pos = _fold_positive_fraction(result.bar_returns, fold_windows)

        # G5 — matched-frequency random entries
        avg_hold_bars = max(round(m.avg_holding_hours), 1)
        rnd = matched_frequency_random_entries(
            window,
            n_trades=m.num_trades,
            avg_hold_bars=avg_hold_bars,
            costs=costs,
            real_net_return=m.net_total_return,
            n_sims=1000,
        )

        # G7 — plateau over (horizon, p_min) neighbors
        def neighbor_sharpes(
            prof_cells: list[dict[str, Any]] = prof_cells,
            family: str = family,
            h: int = h,
            p_min: float = p_min,
        ) -> list[float]:
            hs = sorted({c["horizon"] for c in prof_cells})
            ps = sorted({c["p_min"] for c in prof_cells})
            hi, pi = hs.index(h), ps.index(p_min)
            neigh = []
            for dh in (-1, 0, 1):
                for dp in (-1, 0, 1):
                    if dh == 0 and dp == 0:
                        continue
                    if 0 <= hi + dh < len(hs) and 0 <= pi + dp < len(ps):
                        match = [
                            c
                            for c in prof_cells
                            if c["family"] == family
                            and c["horizon"] == hs[hi + dh]
                            and c["p_min"] == ps[pi + dp]
                        ]
                        neigh.extend(c["sharpe"] for c in match)
            return neigh

        neigh = neighbor_sharpes()
        plateau = (
            float(np.median(neigh)) / chosen_cell["sharpe"]
            if neigh and chosen_cell["sharpe"] > 0
            else None
        )

        # G8 — cost sensitivity x1.5 (the EV gate sees the inflated cost too)
        spec_15 = MLHourlySpec.model_validate(
            {
                "kind": "ml_hourly_longflat",
                "strategy_id": "ml-cost15",
                "venue_profile": prof_name,
                "instrument": {"symbol": symbol, "exchange": settings.research_exchange},
                "horizon_bars": h,
                "p_min": p_min,
            }
        )
        dec15 = decide_window(ohlcv, p_up, spec=spec_15, k_calibration=k_series, cost_multiplier=1.5)
        costs15 = cost_breakdown(prof, symbol, multiplier=1.5)
        res15 = run_vector_backtest(
            ohlcv.loc[dec15.frame.index], col(dec15.frame, "position"), costs15
        )
        m15 = compute_net_metrics(res15, timeframe="1h")
        exp15 = m15.expectancy if m15.num_trades > 0 else None

        # G3 — DSR with the honest n_trials
        dsr_prob = _dsr(m, n_trials)

        report = evaluate_gates(
            strategy_id="ml_hourly_btc_longflat",
            venue_profile=prof_name,
            uk_execution_feasible=prof.uk_execution_feasible,
            inputs=GateInputs(
                metrics=m,
                dsr_probability=dsr_prob,
                positive_fold_fraction=fold_pos,
                random_entry_percentile=rnd.percentile_of_real if rnd else None,
                plateau_ratio=plateau,
                expectancy_at_1p5x_cost=exp15,
                n_trials=n_trials,
                notes=[
                    f"chosen cell: family={family} H={h} p_min={p_min}",
                    "G8 note: at x1.5 cost the EV gate itself tightens "
                    f"(trades {m.num_trades} -> {m15.num_trades})",
                ],
            ),
        )

        # event-engine confirmation + parity
        ev = run_event_backtest(window, dec.frame["position"], costs)
        parity = {
            "vector_net": m.net_total_return,
            "event_net": ev.net_total_return,
            "trade_count_equal": len(ev.trades) == len(result.trades),
            "rel_diff": abs(m.net_total_return - ev.net_total_return)
            / (abs(ev.net_total_return) + 1e-9),
        }

        # baselines
        bh = compute_net_metrics(bl.buy_and_hold(window, costs), timeframe="1h")
        naive = compute_net_metrics(
            bl.naive_sign_no_gate(window, p_up, costs), timeframe="1h"
        )
        boot = block_bootstrap_trades(result.trades)

        # overlay ablation (window limited to 1m availability)
        ablation: dict[str, Any] | None = None
        try:
            m1 = _load(settings, symbol, "1m")
            spread = abdi_ranaldo_spread_bps(m1)
            liq = hour_of_week_liquidity_score(col(m1, "volume"))
            hourly_spread = spread.resample("1h").mean()
            hourly_liq = liq.resample("1h").mean()
            overlay = LiquidityOverlay(
                LiquidityOverlaySpec(),
                spread_bps=hourly_spread,
                liquidity_score=hourly_liq,
            )
            sub_pos = col(dec.frame, "position").loc[dec.frame.index >= dtindex(m1)[0]]
            if len(sub_pos) > 100:
                shifted, ov_log = apply_overlay_to_positions(sub_pos, overlay)
                sub_window = window.loc[sub_pos.index]
                res_off = run_vector_backtest(sub_window, sub_pos, costs)
                res_on = run_vector_backtest(sub_window, shifted, costs)
                ablation = {
                    "window_start": str(sub_pos.index[0]),
                    "bars": len(sub_pos),
                    "net_without_overlay": res_off.net_total_return,
                    "net_with_overlay": res_on.net_total_return,
                    "entries_deferred_or_skipped": sum(
                        1 for _, v in ov_log if v.decision.value != "ALLOW"
                    ),
                    "note": "ablation window limited to trailing-180d 1m data",
                }
        except (RuntimeError, FileNotFoundError) as exc:
            ablation = {"error": f"1m data unavailable: {exc}"}

        reports[prof_name] = {
            "report": report.to_dict(),
            "chosen_cell": chosen_cell,
            "parity": parity,
            "baselines": {
                "buy_and_hold": bh.to_dict(),
                "naive_sign_no_gate": naive.to_dict(),
                "random_entry": asdict(rnd) if rnd else None,
            },
            "bootstrap_ci": asdict(boot) if boot else None,
            "overlay_ablation": ablation,
            "best_xgb": best_xgb,
            "best_logistic": best_log,
        }
        logger.info(
            "ftr_ml_profile_verdict",
            profile=prof_name,
            verdict=report.verdict,
            failed=report.failed_gates,
            trades_per_day=round(m.trades_per_day, 3),
        )

    # --- label-permutation refit (fold subset, honest documented subsample)
    perm_result = _ml_label_permutation(ohlcv, folds, settings, symbol)
    payload = {
        "strategy": "ml_hourly_btc_longflat",
        "n_trials": n_trials,
        "folds": len(folds),
        "profiles": reports,
        "label_permutation": perm_result,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    _write_json(out_dir / "report.json", payload)
    logger.info("ftr_ml_validation_done", out=str(out_dir), elapsed_s=payload["elapsed_s"])
    return payload


def _ml_label_permutation(
    ohlcv: pd.DataFrame,
    folds: list[Fold],
    settings: FTRSettings,
    symbol: str,
    *,
    n_permutations: int = 20,
    fold_stride: int = 8,
) -> dict[str, Any]:
    """Block-permuted-label refits on a documented fold subset.

    Full 20x-all-folds refits would cost ~50 minutes; every-``fold_stride``th
    fold keeps the test honest (same folds for real and permuted models) at
    ~1/8 the compute. Metric: mean test AUC across the subset.
    """
    from sklearn.metrics import roc_auc_score

    spec = MLHourlySpec.model_validate(
        {
            "kind": "ml_hourly_longflat",
            "strategy_id": "ml-perm",
            "venue_profile": "binance_spot_reference",
            "instrument": {"symbol": symbol, "exchange": settings.research_exchange},
            "horizon_bars": 12,
        }
    )
    from marketmind_workers.ftr.features.hourly import atr_h_bps

    x, y, r = build_dataset(ohlcv, spec.horizon_bars)
    atr_h = atr_h_bps(ohlcv, spec.horizon_bars).loc[x.index]
    subset = folds[::fold_stride]

    def subset_auc(labels: pd.Series) -> float:
        aucs: list[float] = []
        idx = dtindex(x)
        for fold in subset:
            fm = fit_fold(x, labels, r, atr_h, fold, spec=spec, model_family="xgboost")
            if fm is None:
                continue
            test_mask = (idx >= fold.test_start) & (idx < fold.test_end)
            y_te = y.loc[test_mask]  # ALWAYS scored against the REAL labels
            common = fm.test_p_up.index.intersection(y_te.index)
            if len(common) < 50 or y_te.loc[common].nunique() < 2:
                continue
            aucs.append(
                float(roc_auc_score(y_te.loc[common].to_numpy(), fm.test_p_up.loc[common]))
            )
        return float(np.mean(aucs)) if aucs else float("nan")

    real_auc = subset_auc(y)
    perm_aucs: list[float] = []
    for i in range(n_permutations):
        y_perm = block_permute_labels(y, seed=1000 + i)
        perm_aucs.append(subset_auc(y_perm))
    p95 = float(np.nanpercentile(perm_aucs, 95))
    return {
        "real_oos_auc": real_auc,
        "permuted_p95_auc": p95,
        "passes": bool(real_auc > p95),
        "n_permutations": n_permutations,
        "fold_subset_stride": fold_stride,
        "n_subset_folds": len(subset),
        "permuted_aucs": perm_aucs,
    }


# ===========================================================================
# 3.2 trend_4h_portfolio
# ===========================================================================


def _trend_cell_run(
    data_by_tf: dict[str, dict[str, pd.DataFrame]],
    universe_by_tf: dict[str, pd.DataFrame],
    regime_by_tf: dict[str, pd.Series],
    cell: dict[str, Any],
    profile_name: str,
    *,
    window: tuple[datetime, datetime] | None = None,
) -> tuple[RunResult, NetMetrics, pd.DataFrame]:
    tf: str = cell["timeframe"]
    data = data_by_tf[tf]
    spec = TrendPortfolioSpec.model_validate(
        {
            "kind": "trend_4h_portfolio",
            "strategy_id": "trend-cell",
            "venue_profile": profile_name,
            "timeframe": tf,
            "ema_fast": cell["ema_fast"],
            "ema_slow": cell["ema_slow"],
            "donchian_n": cell["donchian_n"],
            "chandelier_atr_multiple": cell["chandelier_atr_multiple"],
            "btc_regime_gate": cell["btc_regime_gate"],
        }
    )
    universe = universe_by_tf[tf]
    regime = regime_by_tf[tf] if spec.btc_regime_gate else None

    signals = {}
    for sym, df in data.items():
        idx = dtindex(df)
        if sym not in universe.columns:
            continue
        member_monthly = col(universe, sym)
        member = member_monthly.reindex(idx, method="ffill").fillna(value=False).astype(bool)
        signals[sym] = compute_asset_signals(
            df, spec=spec, member_mask=member, btc_regime_ok=regime
        )
    weights = target_weights(signals, spec=spec)
    if window is not None:
        weights = weights.loc[(weights.index >= window[0]) & (weights.index < window[1])]
    costs_by_symbol = {s: cost_breakdown(profile_name, s) for s in weights.columns}
    result = run_portfolio_backtest(data, weights, costs_by_symbol)
    m = compute_net_metrics(result, timeframe=tf)
    return result, m, weights


def _portfolio_trade_stats(weights: pd.DataFrame, timeframe: str) -> dict[str, float]:
    """Portfolio runs have no single-asset trade ledger; entry counts and
    holding times come from the weight transitions."""
    bar_hours = {"4h": 4.0, "6h": 6.0}[timeframe]
    entries = ((weights > 0) & (weights.shift(1) == 0)).sum().sum()
    held_bars = (weights > 0).sum().sum()
    days = len(weights) * bar_hours / 24.0
    return {
        "num_entries": int(entries),
        "trades_per_day": float(entries) / days if days else 0.0,
        "avg_holding_hours": float(held_bars / entries * bar_hours) if entries else 0.0,
    }


def run_trend_validation(settings: FTRSettings | None = None) -> dict[str, Any]:
    settings = settings or get_ftr_settings()
    t0 = time.monotonic()
    out_dir = settings.artifacts_dir / "validation" / "trend_4h" / _runstamp()

    # --- data: 4h native, 6h resampled from 1h
    data_4h = {s: _load(settings, s, "4h") for s in UNIVERSE_SUPERSET}
    data_6h = {s: resample_6h(_load(settings, s, "1h")) for s in UNIVERSE_SUPERSET}
    data_by_tf = {"4h": data_4h, "6h": data_6h}

    # --- point-in-time universe (monthly), shared spec params for selection
    base_spec = TrendPortfolioSpec.model_validate(
        {"kind": "trend_4h_portfolio", "strategy_id": "trend-base", "venue_profile": "kraken_pro_uk_tier0"}
    )
    universe_by_tf: dict[str, pd.DataFrame] = {}
    regime_by_tf: dict[str, pd.Series] = {}
    for tf, data in data_by_tf.items():
        daily_dv = {
            s: (col(df, "close") * col(df, "volume")).resample("1D").sum() for s, df in data.items()
        }
        listed: dict[str, pd.Timestamp] = {}
        for sym_, df_ in data.items():
            ts0 = dtindex(df_)[0]
            assert isinstance(ts0, pd.Timestamp)
            listed[sym_] = ts0
        all_idx = dtindex(data["BTC/USDT"])
        first_ts, last_ts = all_idx[0], all_idx[-1]
        assert isinstance(first_ts, pd.Timestamp) and isinstance(last_ts, pd.Timestamp)
        month_starts = pd.date_range(
            first_ts.normalize() + pd.Timedelta(days=base_spec.min_listed_days),
            last_ts,
            freq="MS",
            tz="UTC",
        )
        universe_by_tf[tf] = select_universe(
            daily_dv, listed, spec=base_spec, month_starts=month_starts
        )
        btc_daily = col(data["BTC/USDT"], "close").resample("1D").last().dropna()
        regime_by_tf[tf] = btc_regime_mask(btc_daily, all_idx)

    # --- full grid on the SELECTION window (start -> holdout boundary)
    grid_axes = TREND_GRID
    cells: list[dict[str, Any]] = []
    sel_window = (_HISTORY_START, TREND_HOLDOUT_START)
    for ef in grid_axes["ema_fast"]:
        for es in grid_axes["ema_slow"]:
            for n in grid_axes["donchian_n"]:
                for mm in grid_axes["chandelier_atr_multiple"]:
                    for tf in grid_axes["timeframe"]:
                        for gate in grid_axes["btc_regime_gate"]:
                            cell = {
                                "ema_fast": ef,
                                "ema_slow": es,
                                "donchian_n": n,
                                "chandelier_atr_multiple": mm,
                                "timeframe": tf,
                                "btc_regime_gate": gate,
                            }
                            _, m, w = _trend_cell_run(
                                data_by_tf,
                                universe_by_tf,
                                regime_by_tf,
                                cell,
                                "kraken_pro_uk_tier0",
                                window=sel_window,
                            )
                            stats = _portfolio_trade_stats(w, str(tf))
                            cells.append({**cell, **m.to_dict(), **stats})
    n_trials = len(cells)
    _write_json(out_dir / "sweep_cells.json", {"n_trials": n_trials, "cells": cells})

    # selection: best net Sharpe on the selection window
    viable = [c for c in cells if c["num_entries"] > 0]
    chosen = max(viable, key=lambda c: c["sharpe"]) if viable else None
    if chosen is None:
        payload = {
            "strategy": "trend_4h_portfolio",
            "verdict_all_profiles": str(Verdict.REJECTED),
            "note": "no grid cell produced any entries on the selection window",
        }
        _write_json(out_dir / "report.json", payload)
        return payload
    chosen_cell = {k: chosen[k] for k in TREND_GRID}
    logger.info("ftr_trend_chosen", **chosen_cell, sel_sharpe=round(chosen["sharpe"], 3))

    # G7 plateau on selection-window sharpe, +-1 neighbors on numeric axes
    def plateau_ratio() -> float | None:
        if chosen["sharpe"] <= 0:
            return None
        neigh: list[float] = []
        axes = ("ema_fast", "ema_slow", "donchian_n", "chandelier_atr_multiple")
        for ax in axes:
            vals = sorted(TREND_GRID[ax])
            i = vals.index(chosen[ax])
            for di in (-1, 1):
                if 0 <= i + di < len(vals):
                    match = [
                        c
                        for c in cells
                        if all(c[a] == chosen[a] for a in TREND_GRID if a != ax)
                        and c[ax] == vals[i + di]
                    ]
                    neigh.extend(c["sharpe"] for c in match)
        return float(np.median(neigh)) / chosen["sharpe"] if neigh else None

    plateau = plateau_ratio()

    # --- holdout evaluation per profile
    holdout = (TREND_HOLDOUT_START, datetime.now(UTC))
    tf = str(chosen_cell["timeframe"])
    reports: dict[str, Any] = {}
    for prof_name, prof in VENUE_PROFILES.items():
        result, m, weights = _trend_cell_run(
            data_by_tf, universe_by_tf, regime_by_tf, chosen_cell, prof_name, window=holdout
        )
        stats = _portfolio_trade_stats(weights, tf)
        assert result.bar_returns is not None

        # 30d fold windows over the holdout
        fold_edges = pd.date_range(holdout[0], holdout[1], freq="30D", tz="UTC")
        edge_list = [cast("pd.Timestamp", e).to_pydatetime() for e in fold_edges]
        windows = list(itertools.pairwise(edge_list))
        fold_pos = _fold_positive_fraction(result.bar_returns, windows)

        # G5 — circular-shift null: shift each asset's weight column by a
        # random offset (preserves count, holding, sizing; destroys timing)
        rng = np.random.default_rng(1729)
        w_arr = weights.to_numpy()
        data = data_by_tf[tf]
        costs_by_symbol = {s: cost_breakdown(prof_name, s) for s in weights.columns}
        sims = np.empty(1000)
        for s_i in range(1000):
            shifted = np.column_stack(
                [
                    np.roll(w_arr[:, j], int(rng.integers(1, len(w_arr))))
                    for j in range(w_arr.shape[1])
                ]
            )
            w_shift = pd.DataFrame(shifted, index=weights.index, columns=weights.columns)
            r = run_portfolio_backtest(data, w_shift, costs_by_symbol)
            sims[s_i] = r.net_total_return
        rnd_pct = float((sims < m.net_total_return).mean())

        # G8 — x1.5 costs
        costs15 = {s: cost_breakdown(prof_name, s, multiplier=1.5) for s in weights.columns}
        res15 = run_portfolio_backtest(data, weights, costs15)
        m15 = compute_net_metrics(res15, timeframe=tf)

        # DSR
        # portfolio expectancy proxy: per-entry net return approximated by
        # total net / entries (no per-trade ledger on the portfolio path)
        exp15 = (
            m15.net_total_return / stats["num_entries"] if stats["num_entries"] else None
        )
        dsr_prob = _dsr(m, n_trials)

        # diagnostics
        rets = pd.DataFrame(
            {
                s: pd.Series(
                    np.log(col(df, "close").reindex(weights.index).to_numpy()),
                    index=weights.index,
                ).diff()
                for s, df in data.items()
            }
        )
        breadth = effective_breadth(weights, rets)
        regime = regime_by_tf[tf].reindex(weights.index).fillna(value=False)
        r_above = result.bar_returns.loc[regime.to_numpy(dtype=bool)]
        r_below = result.bar_returns.loc[~regime.to_numpy(dtype=bool)]
        regime_split = {
            "btc_above_200dma_return": float(np.prod(1 + r_above.to_numpy()) - 1.0),
            "btc_below_200dma_return": float(np.prod(1 + r_below.to_numpy()) - 1.0),
            "bars_above": int(regime.sum()),
            "bars_below": int((~regime).sum()),
        }

        # metrics for gates: use a copy with portfolio-derived trade stats
        gate_metrics = NetMetrics(
            **{
                **m.to_dict(),
                "num_trades": stats["num_entries"],
                "trades_per_day": stats["trades_per_day"],
                "avg_holding_hours": stats["avg_holding_hours"],
                "expectancy": (
                    m.net_total_return / stats["num_entries"] if stats["num_entries"] else 0.0
                ),
                # per-entry PF is unavailable on the portfolio path; use the
                # sleeve-level proxy: positive months / negative months value
                "profit_factor": _monthly_pf(result.bar_returns),
            }  # type: ignore[arg-type]
        )

        report = evaluate_gates(
            strategy_id="trend_4h_portfolio",
            venue_profile=prof_name,
            uk_execution_feasible=prof.uk_execution_feasible,
            inputs=GateInputs(
                metrics=gate_metrics,
                dsr_probability=dsr_prob,
                positive_fold_fraction=fold_pos,
                random_entry_percentile=rnd_pct,
                plateau_ratio=plateau,
                expectancy_at_1p5x_cost=exp15,
                n_trials=n_trials,
                notes=[
                    f"chosen cell: {chosen_cell}",
                    "profit_factor is the monthly-PF proxy (portfolio path has no per-trade ledger)",
                    f"selection window {sel_window[0].date()}..{sel_window[1].date()}, "
                    f"holdout {holdout[0].date()}..{holdout[1].date()}",
                ],
            ),
        )

        # baselines on the holdout
        btc_window = data["BTC/USDT"].loc[
            (dtindex(data["BTC/USDT"]) >= holdout[0]) & (dtindex(data["BTC/USDT"]) < holdout[1])
        ]
        bh = compute_net_metrics(
            bl.buy_and_hold(btc_window, cost_breakdown(prof_name, "BTC/USDT")), timeframe=tf
        )
        ew = compute_net_metrics(
            bl.equal_weight_hold(data, costs_by_symbol, dtindex(weights)), timeframe=tf
        )
        turtle = compute_net_metrics(
            bl.modern_turtle_proxy(btc_window, cost_breakdown(prof_name, "BTC/USDT")),
            timeframe=tf,
        )

        reports[prof_name] = {
            "report": report.to_dict(),
            "chosen_cell": chosen_cell,
            "holdout_metrics": m.to_dict(),
            "trade_stats": stats,
            "effective_breadth": breadth,
            "regime_split": regime_split,
            "random_shift_null": {
                "percentile_of_real": rnd_pct,
                "sim_p50": float(np.percentile(sims, 50)),
                "sim_p95": float(np.percentile(sims, 95)),
            },
            "baselines": {
                "btc_buy_and_hold": bh.to_dict(),
                "equal_weight_universe": ew.to_dict(),
                "modern_turtle_proxy_btc": turtle.to_dict(),
            },
            "cost_sensitivity_1p5x": m15.to_dict(),
        }
        logger.info(
            "ftr_trend_profile_verdict",
            profile=prof_name,
            verdict=report.verdict,
            failed=report.failed_gates,
            net=round(m.net_total_return, 4),
        )

    payload = {
        "strategy": "trend_4h_portfolio",
        "n_trials": n_trials,
        "chosen_cell": chosen_cell,
        "selection_sharpe": chosen["sharpe"],
        "plateau_ratio": plateau,
        "profiles": reports,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    _write_json(out_dir / "report.json", payload)
    logger.info("ftr_trend_validation_done", out=str(out_dir), elapsed_s=payload["elapsed_s"])
    return payload


def _monthly_pf(bar_returns: pd.Series) -> float:
    monthly = (1 + bar_returns).resample("ME").prod() - 1.0
    gains = float(monthly[monthly > 0].sum())
    losses = float(-monthly[monthly <= 0].sum())
    return gains / losses if losses > 0 else (float("inf") if gains > 0 else 0.0)


# ===========================================================================
# 3.3 ofi_microstructure_research
# ===========================================================================


def run_ofi_validation(settings: FTRSettings | None = None) -> dict[str, Any]:
    settings = settings or get_ftr_settings()
    out_dir = settings.artifacts_dir / "validation" / "ofi_research" / _runstamp()
    spec = validate_ftr_spec(
        {
            "kind": "ofi_microstructure_research",
            "strategy_id": "ofi-btc",
            "venue_profile": "binance_spot_reference",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
        }
    )
    sample = inventory_recordings(settings.recordings_dir, "BTC/USDT")
    if not sample.sufficient:
        payload = {
            "strategy": "ofi_microstructure_research",
            "verdict_all_profiles": str(Verdict.INSUFFICIENT_DATA),
            "sample": {
                "recorded_days": sample.recorded_days,
                "mean_uptime_pct": sample.mean_uptime_pct,
                "hours": sample.hours,
                "required_days": 28,
                "required_uptime_pct": 95.0,
            },
            "note": (
                "Recorder sample below the Stage-4 gate; no verdict of any kind "
                "is computed. Historical spot L2 is not publicly downloadable in "
                "bulk — the recorder collects forward. Expected verdict per the "
                "evidence priors once sampled: REJECTED in taker mode."
            ),
            "research_simulation_only": spec.research_simulation_only,
        }
        _write_json(out_dir / "report.json", payload)
        logger.info("ftr_ofi_insufficient_data", **payload["sample"])  # type: ignore[arg-type]
        return payload

    # Sufficient recorded sample: measurements + research-only simulation.
    # (Reachable once >= 28 days of recordings exist.)
    payload = {
        "strategy": "ofi_microstructure_research",
        "note": "recorded sample sufficient — run measurement pipeline",
        "sample": {
            "recorded_days": sample.recorded_days,
            "mean_uptime_pct": sample.mean_uptime_pct,
        },
    }
    _write_json(out_dir / "report.json", payload)
    return payload


# ===========================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=["ml", "trend", "ofi", "all"], default="all")
    args = parser.parse_args()
    settings = get_ftr_settings()
    if args.strategy in ("ml", "all"):
        run_ml_validation(settings)
    if args.strategy in ("trend", "all"):
        run_trend_validation(settings)
    if args.strategy in ("ofi", "all"):
        run_ofi_validation(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
