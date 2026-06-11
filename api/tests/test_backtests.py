"""Tests for the Phase 3.2 backtest endpoints.

DB reads are monkey-patched at the route module level; queue uses
fakeredis. We never construct a real BacktestResult here because the
shape is large — we only need it to round-trip through the response
serializer, so we build a minimal-but-valid instance via the schema's
default-friendly fields.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_api.routes import backtests as bt_routes
from marketmind_shared.schemas import (
    AuthorClaimComparison,
    BacktestMeta,
    BacktestMetrics,
    BacktestResult,
    BacktestRun,
    BenchmarkComparison,
    BenchmarkEquityPoint,
    BenchmarkResult,
    EquityPoint,
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
)
from marketmind_shared.schemas.strategy_spec import (
    Direction,
    StrategySpec,
    Timeframe,
)
from rq import Queue

# ---- Builders --------------------------------------------------------------


def _refusal_result() -> ExtractionResult:
    return ExtractionResult(
        spec=None,
        report=ExtractionReport(
            verdict=ExtractionVerdict.NOT_EXTRACTABLE,
            overall_confidence=0.05,
            summary="discretionary",
            extracted_rules=[],
            backtestable_parts=[],
            non_backtestable_parts=["x"],
            author_claims=[],
            reasoning="r",
            refusal_explanation="why",
        ),
    )


def _ok_extraction() -> ExtractionResult:
    # SMA-cross spec — matches tests/fixtures/strategies/valid/01_golden_cross.json.
    spec_dict: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "Test",
        "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
        "primary_timeframe": "1d",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "crossover",
                "series": {"kind": "indicator", "name": "sma", "params": {"period": 50}},
                "threshold": {"kind": "indicator", "name": "sma", "params": {"period": 200}},
                "direction": "above",
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {
                    "type": "condition",
                    "condition": {
                        "type": "crossover",
                        "series": {"kind": "indicator", "name": "sma", "params": {"period": 50}},
                        "threshold": {
                            "kind": "indicator",
                            "name": "sma",
                            "params": {"period": 200},
                        },
                        "direction": "below",
                    },
                },
            ],
        },
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
    }
    spec = StrategySpec.model_validate(spec_dict)
    return ExtractionResult(
        spec=spec,
        report=ExtractionReport(
            verdict=ExtractionVerdict.FULLY_EXTRACTABLE,
            overall_confidence=0.9,
            summary="sma cross",
            extracted_rules=[],
            backtestable_parts=["entry", "exit"],
            non_backtestable_parts=[],
            author_claims=[],
            reasoning="explicit",
            refusal_explanation=None,
        ),
    )


def _bt_result(num_curve_points: int = 10) -> BacktestResult:
    spec = _ok_extraction().spec
    assert spec is not None
    start = datetime(2024, 1, 1, tzinfo=UTC)
    curve = [
        EquityPoint(timestamp=start + timedelta(days=i), value=10_000.0 + i * 10.0)
        for i in range(num_curve_points)
    ]
    bench_curve = [BenchmarkEquityPoint(timestamp=p.timestamp, value=p.value * 0.9) for p in curve]
    meta = BacktestMeta(
        symbol="BTC/USDT",
        primary_timeframe=Timeframe.D1,
        filter_timeframe=None,
        start=start,
        end=start + timedelta(days=num_curve_points),
        initial_capital=10_000.0,
        direction=Direction.LONG,
        defaulted_costs=True,
        defaulted_position_sizing=True,
    )
    run = BacktestRun(spec_name="test", meta=meta, equity_curve=curve, trades=[])
    metrics = BacktestMetrics(
        total_return_pct=0.10,
        cagr=0.10,
        annualized_volatility=0.2,
        sharpe_ratio=0.8,
        sortino_ratio=1.0,
        max_drawdown_pct=0.05,
        max_drawdown_duration_days=3,
        calmar_ratio=2.0,
        num_trades=2,
        win_rate=0.5,
        profit_factor=1.1,
        profit_factor_capped=False,
        avg_win_pct=0.04,
        avg_loss_pct=-0.03,
        expectancy=0.005,
        largest_win_pct=0.05,
        largest_loss_pct=-0.04,
        longest_winning_streak=1,
        longest_losing_streak=1,
        avg_trade_duration_days=2.0,
        exposure_pct=0.5,
        bars_processed=num_curve_points,
        bars_per_year=365.0,
    )
    bench = BenchmarkResult(
        total_return_pct=0.05,
        cagr=0.05,
        max_drawdown_pct=0.10,
        sharpe_ratio=0.3,
        final_value=10_500.0,
        initial_value=10_000.0,
        equity_curve=bench_curve,
    )
    comparison = BenchmarkComparison(
        strategy_return_pct=0.10,
        benchmark_return_pct=0.05,
        alpha_pct=0.05,
        beat_benchmark=True,
        strategy_sharpe=0.8,
        benchmark_sharpe=0.3,
        risk_adjusted_alpha=0.5,
        verdict="The strategy outperformed buy-and-hold by 5.00%.",
    )
    return BacktestResult(
        spec_snapshot=spec,
        run=run,
        metrics=metrics,
        benchmark=bench,
        benchmark_comparison=comparison,
        author_claim_comparisons=[],
        data_fetch_seconds=0.0,
        compute_seconds=0.1,
    )


# ---- POST /strategies/{id}/backtest ---------------------------------------


def test_post_backtest_enqueues_when_no_existing(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_id = uuid4()
    monkeypatch.setattr(bt_routes, "fetch_extraction_by_id", lambda _u, _id: _ok_extraction())
    monkeypatch.setattr(bt_routes, "fetch_backtest_for_params", lambda _u, **_: None)

    body = {
        "start": "2024-01-01T00:00:00+00:00",
        "end": "2024-12-31T00:00:00+00:00",
        "initial_capital": 10_000.0,
    }
    resp = client.post(f"/strategies/{strategy_id}/backtest", json=body)
    assert resp.status_code == 202, resp.text
    j = resp.json()
    assert j["from_cache"] is False
    assert j["job_id"]
    assert j["backtest_id"] is None

    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 1


def test_post_backtest_idempotent_hit(
    client: TestClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_id = uuid4()
    bt_id = uuid4()
    monkeypatch.setattr(bt_routes, "fetch_extraction_by_id", lambda _u, _id: _ok_extraction())
    monkeypatch.setattr(
        bt_routes,
        "fetch_backtest_for_params",
        lambda _u, **_: (bt_id, _bt_result()),
    )

    body = {"start": "2024-01-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"}
    resp = client.post(f"/strategies/{strategy_id}/backtest", json=body)
    assert resp.status_code == 202
    j = resp.json()
    assert j["from_cache"] is True
    assert j["backtest_id"] == str(bt_id)
    queue = Queue(name="default", connection=fake_redis)
    assert queue.count == 0


def test_post_backtest_404_when_strategy_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bt_routes, "fetch_extraction_by_id", lambda _u, _id: None)
    body = {"start": "2024-01-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"}
    resp = client.post(f"/strategies/{uuid4()}/backtest", json=body)
    assert resp.status_code == 404


def test_post_backtest_422_when_spec_missing(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bt_routes, "fetch_extraction_by_id", lambda _u, _id: _refusal_result())
    body = {"start": "2024-01-01T00:00:00+00:00", "end": "2024-12-31T00:00:00+00:00"}
    resp = client.post(f"/strategies/{uuid4()}/backtest", json=body)
    assert resp.status_code == 422


def test_post_backtest_422_when_end_before_start(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bt_routes, "fetch_extraction_by_id", lambda _u, _id: _ok_extraction())
    monkeypatch.setattr(bt_routes, "fetch_backtest_for_params", lambda _u, **_: None)
    body = {"start": "2024-12-31T00:00:00+00:00", "end": "2024-01-01T00:00:00+00:00"}
    resp = client.post(f"/strategies/{uuid4()}/backtest", json=body)
    assert resp.status_code == 422


# ---- GET /backtests/{id} --------------------------------------------------


def test_get_backtest_returns_downsampled_curve(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bt_id = uuid4()
    # 5000 points -> must be downsampled to <= 500.
    big_result = _bt_result(num_curve_points=5000)
    monkeypatch.setattr(
        bt_routes,
        "fetch_backtest_by_id",
        lambda _u, _id: (uuid4(), big_result, datetime(2024, 1, 1, tzinfo=UTC)),
    )
    resp = client.get(f"/backtests/{bt_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["run"]["equity_curve"]) <= 500
    assert len(body["benchmark"]["equity_curve"]) <= 500


def test_get_backtest_404(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bt_routes, "fetch_backtest_by_id", lambda _u, _id: None)
    resp = client.get(f"/backtests/{uuid4()}")
    assert resp.status_code == 404


# ---- GET /strategies/{id}/backtests ---------------------------------------


def test_list_backtests_returns_items(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy_id = uuid4()
    bt_id = uuid4()

    def fake_list(_url: str, _sid: Any, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return [
            {
                "backtest_id": bt_id,
                "created_at": datetime(2026, 5, 1, tzinfo=UTC),
                "start_ts": datetime(2024, 1, 1, tzinfo=UTC),
                "end_ts": datetime(2024, 12, 31, tzinfo=UTC),
                "initial_capital": 10_000.0,
                "result": _bt_result(num_curve_points=2000),
            },
        ]

    monkeypatch.setattr(bt_routes, "list_backtests_for_strategy", fake_list)
    resp = client.get(f"/strategies/{strategy_id}/backtests")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 20
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["backtest_id"] == str(bt_id)
    assert len(item["result"]["run"]["equity_curve"]) <= 500


# pyright-friendly: silence "imported but unused" warnings
_ = AuthorClaimComparison
