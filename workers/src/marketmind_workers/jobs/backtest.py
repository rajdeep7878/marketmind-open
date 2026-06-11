"""RQ job: end-to-end backtest of a previously-extracted strategy.

Inputs (kwargs from enqueue):
  - strategy_id (uuid str)
  - start_iso  (UTC ISO datetime)
  - end_iso    (UTC ISO datetime)
  - initial_capital (float, optional, default 10_000)

Outputs (return dict):
  - backtest_id (uuid str)
  - from_cache  (bool)
  - alpha_pct   (float) — strategy_return minus benchmark_return
  - beat_benchmark (bool)

Side effects:
  - One row in `backtest_results`. Idempotent on
    (strategy_id, start, end, initial_capital).

Phase 3.2 scope:
  - Single-instrument only — driven by the spec's `instrument.symbol`.
  - Honest backtest: real fees, slippage, next-bar-open fills (engine).
  - Buy-and-hold benchmark on the same window with the same fees.
  - Author claims compared per-claim against measured metrics.
  - NO overfitting analysis here (that's Phase 4).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from uuid import UUID

import structlog
from marketmind_shared.schemas import BacktestResult

from marketmind_workers.backtest.author_comparison import compare_author_claims
from marketmind_workers.backtest.benchmark import (
    compare_to_benchmark,
    compute_buy_and_hold,
)
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.fee_model import commission_for_spec
from marketmind_workers.backtest.metrics import compute_metrics
from marketmind_workers.backtest.slippage_model import slippage_for_spec
from marketmind_workers.config import get_settings
from marketmind_workers.db import (
    fetch_backtest_for_params,
    fetch_extraction_by_id,
    save_backtest_result,
)

log = structlog.get_logger(__name__)


def run(
    strategy_id: str,
    start_iso: str,
    end_iso: str,
    initial_capital: float = 10_000.0,
) -> dict[str, Any]:
    settings = get_settings()
    database_url = str(settings.database_url)
    data_dir = settings.data_dir

    sid = UUID(strategy_id)
    start_dt = datetime.fromisoformat(start_iso)
    end_dt = datetime.fromisoformat(end_iso)

    log.info(
        "backtest_starting",
        strategy_id=strategy_id,
        start=start_iso,
        end=end_iso,
        initial_capital=initial_capital,
    )

    # ---- idempotent fast path ------------------------------------------------
    existing = fetch_backtest_for_params(
        database_url,
        strategy_id=sid,
        start_ts=start_dt,
        end_ts=end_dt,
        initial_capital=initial_capital,
    )
    if existing is not None:
        backtest_id, prior = existing
        log.info("backtest_cache_hit", backtest_id=str(backtest_id), strategy_id=strategy_id)
        return {
            "backtest_id": str(backtest_id),
            "from_cache": True,
            "alpha_pct": prior.benchmark_comparison.alpha_pct,
            "beat_benchmark": prior.benchmark_comparison.beat_benchmark,
        }

    # ---- load + validate strategy --------------------------------------------
    extraction = fetch_extraction_by_id(database_url, sid)
    if extraction is None:
        raise ValueError(f"no extracted_strategy with id={strategy_id}")
    _transcript_id, extraction_result = extraction
    spec = extraction_result.spec
    if spec is None:
        raise ValueError(
            f"strategy {strategy_id} has no StrategySpec (verdict={extraction_result.report.verdict});"
            " cannot backtest a refused extraction",
        )

    # ---- engine --------------------------------------------------------------
    t_fetch_start = time.perf_counter()
    run_obj = run_backtest(spec, start_dt, end_dt, initial_capital, data_dir=data_dir)
    t_engine_done = time.perf_counter()

    # ---- analyses ------------------------------------------------------------
    metrics = compute_metrics(run_obj, spec.primary_timeframe)
    benchmark = compute_buy_and_hold(
        spec.instrument.symbol,
        spec.primary_timeframe,
        start_dt,
        end_dt,
        initial_capital=initial_capital,
        commission_pct=commission_for_spec(spec),
        slippage_pct=slippage_for_spec(spec),
        data_dir=data_dir,
    )
    benchmark_comparison = compare_to_benchmark(metrics, benchmark)
    author_comparisons = compare_author_claims(
        list(extraction_result.report.author_claims),
        metrics,
        spec_symbol=spec.instrument.symbol,
        backtest_start=start_dt,
        backtest_end=end_dt,
    )
    t_analyses_done = time.perf_counter()

    # The split data_fetch vs compute timing is approximate (the engine
    # bundles both inside run_backtest). Use total engine time as
    # data_fetch + compute split is opaque from outside; report total
    # as compute and 0 for fetch for now. Phase 4 may want a true split.
    compute_seconds = t_analyses_done - t_engine_done + (t_engine_done - t_fetch_start)
    result = BacktestResult(
        spec_snapshot=spec,
        run=run_obj,
        metrics=metrics,
        benchmark=benchmark,
        benchmark_comparison=benchmark_comparison,
        author_claim_comparisons=author_comparisons,
        data_fetch_seconds=0.0,
        compute_seconds=compute_seconds,
    )

    backtest_id = save_backtest_result(
        database_url,
        strategy_id=sid,
        start_ts=start_dt,
        end_ts=end_dt,
        initial_capital=initial_capital,
        result=result,
    )

    log.info(
        "backtest_complete",
        backtest_id=str(backtest_id),
        strategy_id=strategy_id,
        total_return_pct=metrics.total_return_pct,
        alpha_pct=benchmark_comparison.alpha_pct,
        beat_benchmark=benchmark_comparison.beat_benchmark,
        num_trades=metrics.num_trades,
        compute_seconds=compute_seconds,
    )
    return {
        "backtest_id": str(backtest_id),
        "from_cache": False,
        "alpha_pct": benchmark_comparison.alpha_pct,
        "beat_benchmark": benchmark_comparison.beat_benchmark,
    }
