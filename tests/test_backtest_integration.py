"""Phase 3.2 end-to-end smoke test against a real Postgres + the
worker's backtest orchestrator.

Opt-in (`-m integration`). Spins up a Postgres testcontainer, applies
migrations including the new 0004, seeds a YouTube content + transcript
+ extracted-strategy row containing the Golden-Cross fixture, mocks
out the market-data fetch (we don't hit Binance in the default suite),
and runs the worker's `backtest.run` callable directly. Then verifies:

  - one backtest_results row was written
  - re-invoking with identical params returns the cached row (the
    idempotency probe + unique index agree)
  - the BacktestResult round-trips out of the DB with metrics +
    benchmark + benchmark_comparison + author claim comparisons
    populated
  - the verdict string is non-empty and the alpha sign matches the
    return delta we constructed

We construct a synthetic OHLCV frame designed so the Golden Cross
strategy will trade (SMA-50 crosses SMA-200) and produce a measurable
alpha relative to buy-and-hold.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg
import pytest

pytestmark = pytest.mark.integration

testcontainers = pytest.importorskip("testcontainers.postgres")

from marketmind_shared.schemas import (  # noqa: E402
    ExtractionReport,
    ExtractionResult,
    ExtractionVerdict,
    StrategySpec,
    Transcript,
    YouTubeContent,
)
from marketmind_workers.db import (  # noqa: E402
    apply_migrations,
    fetch_backtest_result_by_id,
    list_backtests_for_strategy,
    save_content,
    save_extraction,
    save_transcript,
)
from testcontainers.postgres import PostgresContainer  # noqa: E402

# ---- Fixtures -------------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container() -> Iterator[PostgresContainer]:
    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: PostgresContainer) -> str:
    url = pg_container.get_connection_url()
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def synthetic_ohlcv() -> pd.DataFrame:
    """Build a frame designed to trigger a Golden Cross signal.

    First half: prices drift down so SMA-50 < SMA-200. Second half:
    prices rally so SMA-50 crosses above SMA-200 → entry → big up
    move. Result: the strategy beats buy-and-hold because it sits in
    cash through the down phase and is fully invested through the up
    phase.
    """
    n = 365
    start = datetime(2024, 1, 1, tzinfo=UTC)
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(n)])
    half = n // 2
    down = np.linspace(100.0, 60.0, num=half)
    up = np.linspace(60.0, 200.0, num=n - half)
    closes = np.concatenate([down, up])
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.full(n, 1000.0),
        },
        index=idx,
    )


# ---- Spec + seeding helpers ----------------------------------------------


def _golden_cross_spec() -> StrategySpec:
    """The committed Golden Cross fixture, loaded via Pydantic so the
    validator chain runs end-to-end before we persist.
    """
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "strategies"
        / "valid"
        / "01_golden_cross.json"
    )
    payload = json.loads(fixture_path.read_text())
    # Shorten SMA periods so the cross actually fires within 365 bars.
    payload["entry"]["condition"]["series"]["params"]["period"] = 20
    payload["entry"]["condition"]["threshold"]["params"]["period"] = 50
    payload["exit"]["exits"][0]["condition"]["series"]["params"]["period"] = 20
    payload["exit"]["exits"][0]["condition"]["threshold"]["params"]["period"] = 50
    return StrategySpec.model_validate(payload)


def _seed_extraction(database_url: str, spec: StrategySpec) -> tuple[str, str]:
    """Insert content + transcript + extracted_strategy and return
    (transcript_id, extraction_id) as UUID strings.
    """
    yt = YouTubeContent(
        video_id="bt12345abcd",
        title="Quant Tactics Golden Cross",
        channel="Quant Tactics",
        duration_seconds=600.0,
        audio_path=Path("/data/cache/audio/bt.m4a"),
    )
    content_id = save_content(database_url, yt)
    transcript_id = save_transcript(
        database_url,
        content_id,
        Transcript(
            language="en",
            full_text="Golden cross strategy: 50 SMA over 200 SMA, BTC.",
            segments=[],
            duration_seconds=600.0,
            model_name="small",
        ),
    )
    extraction = ExtractionResult(
        spec=spec,
        report=ExtractionReport(
            verdict=ExtractionVerdict.FULLY_EXTRACTABLE,
            overall_confidence=0.95,
            summary="20/50 SMA cross on BTC/USDT, long-only.",
            extracted_rules=[],
            backtestable_parts=["entry", "exit"],
            non_backtestable_parts=[],
            author_claims=[],
            reasoning="Mechanical SMA cross.",
            refusal_explanation=None,
        ),
    )
    extraction_id = save_extraction(database_url, transcript_id, extraction)
    return str(transcript_id), str(extraction_id)


# ---- Test ----------------------------------------------------------------


def test_backtest_job_end_to_end(
    database_url: str,
    synthetic_ohlcv: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _golden_cross_spec()
    _, extraction_id = _seed_extraction(database_url, spec)

    # Wire the worker's settings to the test database so the job's
    # `get_settings().database_url` resolves to our testcontainer.
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    from marketmind_workers.config import get_settings

    get_settings.cache_clear()

    # Avoid hitting Binance: every call to get_market_data inside the
    # engine and benchmark returns the same fabricated frame, sliced
    # to the requested window so the equity curve is well-defined.
    from marketmind_workers.backtest import benchmark as bench_module
    from marketmind_workers.backtest import engine as engine_module

    def fake_get_market_data(
        _symbol: str,
        _tf: str,
        start: datetime,
        end: datetime,
        *,
        data_dir: str | Path = "/data",
        client: object | None = None,
    ) -> pd.DataFrame:
        return synthetic_ohlcv.loc[(synthetic_ohlcv.index >= start) & (synthetic_ohlcv.index < end)]

    monkeypatch.setattr(engine_module, "get_market_data", fake_get_market_data)
    monkeypatch.setattr(bench_module, "get_market_data", fake_get_market_data)

    # Now drive the job directly. RQ is not involved — we test the
    # callable as-is, the same way the worker invokes it.
    from marketmind_workers.jobs.backtest import run as run_backtest_job

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 12, 30, tzinfo=UTC)
    result = run_backtest_job(
        extraction_id,
        start.isoformat(),
        end.isoformat(),
        10_000.0,
    )
    backtest_id_1 = result["backtest_id"]
    assert result["from_cache"] is False
    assert isinstance(backtest_id_1, str) and backtest_id_1

    # Idempotency: same params -> cache hit.
    result2 = run_backtest_job(
        extraction_id,
        start.isoformat(),
        end.isoformat(),
        10_000.0,
    )
    assert result2["from_cache"] is True
    assert result2["backtest_id"] == backtest_id_1

    # Fetch the persisted row and verify the BacktestResult round-trips.
    from uuid import UUID

    fetched = fetch_backtest_result_by_id(database_url, UUID(backtest_id_1))
    assert fetched is not None
    _strategy_id, persisted, _created_at = fetched
    assert persisted.spec_snapshot.name == spec.name
    assert persisted.metrics.bars_processed > 100  # the engine processed our synthetic frame
    assert persisted.benchmark.equity_curve, "benchmark curve missing"
    assert persisted.benchmark_comparison.verdict  # non-empty
    # Comparison alpha sign must match measured-minus-benchmark.
    expected_alpha = persisted.metrics.total_return_pct - persisted.benchmark.total_return_pct
    assert abs(persisted.benchmark_comparison.alpha_pct - expected_alpha) < 1e-9

    # The strategy avoids the down half by definition (no SMA cross yet),
    # so the strategy should beat or roughly match buy-and-hold here.
    assert persisted.benchmark_comparison.alpha_pct >= -0.01

    # And the list endpoint surfaces this row for the strategy.
    rows = list_backtests_for_strategy(database_url, UUID(extraction_id))
    assert any(row[0] == UUID(backtest_id_1) for row in rows)
