"""RQ job: run the full overfitting analysis pipeline against a
previously-persisted BacktestResult.

Pipeline:
  1. Load BacktestResult + spec from backtest_results table.
  2. Idempotency probe — if an analysis already exists for this
     backtest_id, return its id immediately.
  3. Run walk_forward → parameter_sweep → monte_carlo → deflated_sharpe.
     After each step, update `current_job.meta["progress"]` so the
     API's /jobs/{id}/progress endpoint can surface where we are.
  4. Compute composite score.
  5. Persist atomically (one row in `overfitting_analyses`).
  6. Return analysis_id + summary.

Compute budget on cached Binance data:
  - walk_forward: 6 windows × 2 segments = 12 backtests
  - parameter_sweep: up to 50 cells
  - monte_carlo: 100 permutations (50 with fallback)
  - deflated_sharpe: 0 backtests (closed-form from existing metrics)
  → ~160 backtests at ~1s each = ~3 minutes.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

import structlog
from marketmind_shared.schemas import OverfittingAnalysis
from rq import get_current_job

from marketmind_workers.config import get_settings
from marketmind_workers.db import (
    fetch_backtest_result_by_id,
    fetch_overfitting_analysis_for_backtest,
    save_overfitting_analysis,
)
from marketmind_workers.overfitting.composite import compute_overfitting_score
from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe
from marketmind_workers.overfitting.monte_carlo import run_monte_carlo
from marketmind_workers.overfitting.parameter_sweep import run_parameter_sweep
from marketmind_workers.overfitting.walk_forward import run_walk_forward

log = structlog.get_logger(__name__)


_PROGRESS_KEY = "marketmind:overfitting:progress"


def _publish_progress(step: str, current: int, total: int) -> None:
    """Write progress to the current RQ job's meta dict. Survives in
    Redis until the job is reaped (default TTL: 3600s).

    Outside an RQ worker (e.g., during unit tests where the function
    runs synchronously) get_current_job() returns None — we silently
    skip.
    """
    job = get_current_job()
    if job is None:
        return
    job.meta[_PROGRESS_KEY] = {"step": step, "current": current, "total": total}
    job.save_meta()


def run(backtest_id: str) -> dict[str, Any]:
    settings = get_settings()
    database_url = str(settings.database_url)
    data_dir = settings.data_dir
    bid = UUID(backtest_id)

    log.info("overfitting_starting", backtest_id=backtest_id)
    t_start = time.perf_counter()

    # ---- idempotent fast path -----------------------------------------------
    existing = fetch_overfitting_analysis_for_backtest(database_url, bid)
    if existing is not None:
        analysis_id, prior = existing
        log.info("overfitting_cache_hit", analysis_id=str(analysis_id))
        return {
            "analysis_id": str(analysis_id),
            "from_cache": True,
            "composite_score": prior.composite.score,
            "verdict": prior.composite.verdict.value,
        }

    # ---- load backtest ------------------------------------------------------
    row = fetch_backtest_result_by_id(database_url, bid)
    if row is None:
        raise ValueError(f"no backtest_result with id={backtest_id}")
    _strategy_id, backtest, _created_at = row
    spec = backtest.spec_snapshot
    start = backtest.run.meta.start
    end = backtest.run.meta.end
    metrics = backtest.metrics

    # ---- step 1: walk-forward ----------------------------------------------
    _publish_progress("walk_forward", 0, 4)
    log.info("overfitting_step_walk_forward")
    wf = run_walk_forward(spec, start, end, data_dir=data_dir)

    # ---- step 2: parameter sweep -------------------------------------------
    _publish_progress("parameter_sweep", 1, 4)
    log.info("overfitting_step_parameter_sweep")
    sweep = run_parameter_sweep(spec, start, end, data_dir=data_dir)

    # ---- step 3: monte carlo -----------------------------------------------
    _publish_progress("monte_carlo", 2, 4)
    log.info("overfitting_step_monte_carlo")
    mc = run_monte_carlo(spec, start, end, data_dir=data_dir)

    # ---- step 4: deflated sharpe -------------------------------------------
    _publish_progress("deflated_sharpe", 3, 4)
    log.info("overfitting_step_deflated_sharpe")
    # `n_trials_estimate=100` is the conservative default for retail
    # strategies — see workers/overfitting/deflated_sharpe.py. We assume
    # normal returns (skew=0, kurt=3) for v1 because we don't surface
    # those moments on the BacktestMetrics yet. Phase 5 should add them.
    #
    # FREQUENCY MISMATCH FIX (2026-05-25, post-DSR-audit): `sharpe_ratio`
    # is ANNUALIZED in metrics.py:169 (`sharpe = mean_r * bpy / vol`),
    # so the PSR/DSR formula's `T` must also be in annual units. Using
    # raw `bars_processed` here inflated sqrt(T-1) by sqrt(bpy) ≈ 47×
    # at 4H and ≈ 187× at 15m, pegging prob_real at ≈ 0 for every
    # strategy in 15 historical gauntlet runs. We divide by the SAME
    # bpy the metrics object carries (line 115 in metrics.py: stored
    # alongside the Sharpe it annualized), guaranteeing sourcing
    # consistency. Floor at 2 to satisfy the >=2 guard in deflated_sharpe.
    t_years = max(metrics.bars_processed / metrics.bars_per_year, 2.0)
    ds = deflated_sharpe(
        metrics.sharpe_ratio,
        n_trials_estimate=100,
        n_observations=round(t_years),
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )

    # ---- composite ---------------------------------------------------------
    _publish_progress("composite", 4, 4)
    composite = compute_overfitting_score(
        spec,
        walk_forward=wf,
        sweep=sweep,
        monte_carlo=mc,
        deflated=ds,
    )

    compute_seconds = time.perf_counter() - t_start
    analysis = OverfittingAnalysis(
        walk_forward=wf,
        parameter_sweep=sweep,
        monte_carlo=mc,
        deflated_sharpe=ds,
        composite=composite,
        compute_seconds=compute_seconds,
    )
    analysis_id = save_overfitting_analysis(
        database_url,
        backtest_id=bid,
        analysis=analysis,
    )

    log.info(
        "overfitting_complete",
        analysis_id=str(analysis_id),
        composite_score=composite.score,
        verdict=composite.verdict.value,
        compute_seconds=compute_seconds,
    )
    return {
        "analysis_id": str(analysis_id),
        "from_cache": False,
        "composite_score": composite.score,
        "verdict": composite.verdict.value,
    }
