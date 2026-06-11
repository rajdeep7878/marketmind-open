"""Tests for the Phase 4 overfitting endpoints.

Reads are monkey-patched at the route module level. We build a
minimal OverfittingAnalysis using the schema's required fields.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_api.routes import overfitting as of_routes
from marketmind_shared.schemas import (
    DeflatedSharpeResult,
    MonteCarloHistogramBin,
    MonteCarloResult,
    OverfittingAnalysis,
    OverfittingScore,
    OverfittingVerdict,
    ParameterSweepResult,
    SignalContribution,
    WalkForwardResult,
)
from rq import Queue


def _analysis() -> OverfittingAnalysis:
    wf = WalkForwardResult(
        windows=[],
        in_sample_avg_return=0.20,
        out_of_sample_avg_return=0.15,
        degradation_ratio=0.75,
        degradation_ratio_valid=True,
        out_of_sample_positive_rate=0.5,
        consistency_score=0.6,
        train_ratio=0.7,
        n_windows_requested=6,
        n_windows_actual=6,
    )
    sw = ParameterSweepResult(
        axes=[],
        cells=[],
        baseline_return_pct=0.20,
        baseline_rank_percentile=0.6,
        best_in_grid_return=0.30,
        worst_in_grid_return=0.10,
        neighborhood_avg_return=0.18,
        peakiness_score=0.2,
        n_combinations=25,
        skipped_reason=None,
    )
    mc = MonteCarloResult(
        real_return_pct=0.20,
        real_sharpe=1.0,
        n_permutations=100,
        synthetic_mean_return=0.0,
        synthetic_std_return=0.1,
        synthetic_min=-0.3,
        synthetic_max=0.4,
        histogram=[MonteCarloHistogramBin(lo=-0.3, hi=0.4, count=100)],
        p_value=0.05,
        percentile_rank=0.95,
        seed=42,
    )
    ds = DeflatedSharpeResult(
        observed_sharpe=1.5,
        deflated_sharpe_ratio=0.5,
        probability_strategy_is_real=0.85,
        n_trials_estimate=100,
        n_observations=1000,
        returns_skewness=0.0,
        returns_kurtosis=3.0,
        expected_max_sharpe=1.0,
        method="lopez_de_prado_full",
    )
    composite = OverfittingScore(
        score=22.5,
        verdict=OverfittingVerdict.LIKELY_ROBUST,
        contributions=[
            SignalContribution(
                name="walk_forward",
                label="Walk-forward degradation",
                raw_value=0.75,
                weight=0.35,
                contribution_pts=15.0,
            ),
            SignalContribution(
                name="parameter_sweep",
                label="Parameter peakiness",
                raw_value=0.2,
                weight=0.25,
                contribution_pts=10.0,
            ),
            SignalContribution(
                name="monte_carlo",
                label="Monte Carlo p-value",
                raw_value=0.05,
                weight=0.25,
                contribution_pts=20.0,
            ),
            SignalContribution(
                name="deflated_sharpe",
                label="Deflated Sharpe probability",
                raw_value=0.85,
                weight=0.15,
                contribution_pts=29.0,
            ),
        ],
        explanation="This strategy looks robust.",
        confidence_band_low=12.5,
        confidence_band_high=32.5,
    )
    return OverfittingAnalysis(
        walk_forward=wf,
        parameter_sweep=sw,
        monte_carlo=mc,
        deflated_sharpe=ds,
        composite=composite,
        compute_seconds=120.0,
    )


# ---- POST /backtests/{backtest_id}/overfitting -----------------------------


def test_post_enqueues_when_no_existing(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bt_id = uuid4()
    monkeypatch.setattr(
        of_routes,
        "fetch_backtest_by_id",
        lambda _u, _id: (uuid4(), object(), datetime(2024, 1, 1, tzinfo=UTC)),
    )
    monkeypatch.setattr(of_routes, "fetch_overfitting_for_backtest", lambda _u, _b: None)

    resp = client.post(f"/backtests/{bt_id}/overfitting")
    assert resp.status_code == 202, resp.text
    j = resp.json()
    assert j["from_cache"] is False
    assert j["job_id"]
    assert j["analysis_id"] is None

    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1


def test_post_idempotent_hit(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bt_id = uuid4()
    analysis_id = uuid4()
    monkeypatch.setattr(
        of_routes,
        "fetch_backtest_by_id",
        lambda _u, _id: (uuid4(), object(), datetime(2024, 1, 1, tzinfo=UTC)),
    )
    monkeypatch.setattr(
        of_routes,
        "fetch_overfitting_for_backtest",
        lambda _u, _b: (analysis_id, _analysis()),
    )

    resp = client.post(f"/backtests/{bt_id}/overfitting")
    assert resp.status_code == 202
    j = resp.json()
    assert j["from_cache"] is True
    assert j["analysis_id"] == str(analysis_id)
    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 0


def test_post_404_when_backtest_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(of_routes, "fetch_backtest_by_id", lambda _u, _id: None)
    resp = client.post(f"/backtests/{uuid4()}/overfitting")
    assert resp.status_code == 404


# ---- GET /overfitting/{analysis_id} ---------------------------------------


def test_get_analysis_happy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        of_routes,
        "fetch_overfitting_by_id",
        lambda _u, _id: (uuid4(), _analysis(), datetime(2024, 1, 1, tzinfo=UTC)),
    )
    resp = client.get(f"/overfitting/{uuid4()}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["composite"]["score"] == 22.5
    assert body["composite"]["verdict"] == "likely_robust"


def test_get_analysis_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(of_routes, "fetch_overfitting_by_id", lambda _u, _id: None)
    resp = client.get(f"/overfitting/{uuid4()}")
    assert resp.status_code == 404


# ---- GET /backtests/{backtest_id}/overfitting ------------------------------


def test_get_for_backtest_when_analysis_exists(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = uuid4()
    monkeypatch.setattr(
        of_routes,
        "fetch_backtest_by_id",
        lambda _u, _id: (uuid4(), object(), datetime(2024, 1, 1, tzinfo=UTC)),
    )
    monkeypatch.setattr(
        of_routes,
        "fetch_overfitting_for_backtest",
        lambda _u, _b: (aid, _analysis()),
    )
    resp = client.get(f"/backtests/{uuid4()}/overfitting")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["analysis_id"] == str(aid)
    assert body["analysis"]["composite"]["score"] == 22.5


def test_get_for_backtest_404_when_no_analysis(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        of_routes,
        "fetch_backtest_by_id",
        lambda _u, _id: (uuid4(), object(), datetime(2024, 1, 1, tzinfo=UTC)),
    )
    monkeypatch.setattr(of_routes, "fetch_overfitting_for_backtest", lambda _u, _b: None)
    resp = client.get(f"/backtests/{uuid4()}/overfitting")
    assert resp.status_code == 404


# ---- /jobs/{id}/progress ---------------------------------------------------


def test_job_progress_returns_meta_when_present(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    # Manually push a job into fake redis with a progress meta key.
    queue = Queue(name="default", connection=fake_redis)
    job = queue.enqueue(
        "marketmind_workers.jobs.dummy.run",
        kwargs={"message": "x"},
        meta={
            "marketmind:overfitting:progress": {
                "step": "monte_carlo",
                "current": 2,
                "total": 4,
            }
        },
    )
    resp = client.get(f"/jobs/{job.id}/progress")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["step"] == "monte_carlo"
    assert body["current"] == 2
    assert body["total"] == 4


def test_job_progress_returns_none_fields_when_no_meta(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    queue = Queue(name="default", connection=fake_redis)
    job = queue.enqueue("marketmind_workers.jobs.dummy.run", kwargs={"message": "x"})
    resp = client.get(f"/jobs/{job.id}/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["step"] is None
    assert body["current"] is None


# pyright silence
_: Any = OverfittingAnalysis
