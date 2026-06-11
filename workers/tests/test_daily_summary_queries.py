"""Daily summary — query layer (integration, testcontainers).

`build_daily_summary` and its query functions are DB-orchestration code;
they are exercised against a real PostgresContainer with a seeded
fixture state. `now` is passed explicitly so the result is deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from marketmind_workers.observability.daily_summary import generate_and_write
from marketmind_workers.observability.models import DailySummary
from marketmind_workers.observability.queries import build_daily_summary

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 5, 22, 0, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def pg_container() -> Iterator[object]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: object) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def conn(database_url: str) -> Iterator[psycopg.Connection[object]]:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cur:
            cur.execute(
                "TRUNCATE trader_strategies, trader_candles, trader_audit_logs, "
                "trader_alerts, trader_bot_runs, trader_portfolio_snapshots "
                "RESTART IDENTITY CASCADE",
            )
        connection.commit()
        yield connection


def _seed_bot_run(conn: psycopg.Connection[object], heartbeat: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_bot_runs (loop_name, started_at, last_heartbeat_at, "
            "status, worker_id) VALUES ('runner', %s, %s, 'running', 'test')",
            (heartbeat - timedelta(hours=1), heartbeat),
        )
    conn.commit()


def _seed_snapshot(conn: psycopg.Connection[object], ts: datetime, equity: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_portfolio_snapshots (ts, cash, equity, unrealised_pnl, "
            "realised_pnl_cumulative, peak_equity, drawdown, drawdown_pct, "
            "open_positions_count) VALUES (%s, %s, %s, 0, 0, %s, 0, 0, 0)",
            (ts, Decimal(equity), Decimal(equity), Decimal(equity)),
        )
    conn.commit()


def _seed_strategy(conn: psycopg.Connection[object], name: str, *, enabled: bool = True) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_strategies (name) VALUES (%s) RETURNING id", (name,),
        )
        sid = cur.fetchone()[0]  # type: ignore[index]
        cur.execute(
            "INSERT INTO trader_strategy_versions (strategy_id, version, marketmind_spec_id, "
            "template, parameters, symbols, timeframes, risk_pct, fee_bps, slippage_bps, "
            "backtest_metrics, approved_for_paper, enabled) "
            "VALUES (%s, 1, %s, 'ma_trend', '{}', %s, %s, 0.01, 10, 10, '{}', true, %s)",
            (str(sid), str(uuid4()), ["BTC/USDT"], ["4h"], enabled),
        )
    conn.commit()


def _seed_candles(conn: psycopg.Connection[object], n: int) -> None:
    with conn.cursor() as cur:
        for i in range(n):
            open_ts = _NOW - timedelta(hours=4 * (n - i))
            cur.execute(
                "INSERT INTO trader_candles (symbol, timeframe, open_ts, close_ts, "
                "open, high, low, close, volume, is_closed, source) VALUES "
                "('BTC/USDT', '4h', %s, %s, 100, 101, 99, 100, 1000, true, 'test')",
                (open_ts, open_ts + timedelta(hours=4)),
            )
    conn.commit()


def test_build_daily_summary_healthy_bot(conn: psycopg.Connection[object]) -> None:
    _seed_bot_run(conn, heartbeat=_NOW - timedelta(seconds=5))
    _seed_snapshot(conn, _NOW - timedelta(hours=25), "1000.00")
    _seed_snapshot(conn, _NOW - timedelta(minutes=1), "1012.50")
    _seed_strategy(conn, "Golden Cross 4H BTC")
    _seed_candles(conn, 300)  # > ma_trend min_bars_needed → EVALUATING

    summary = build_daily_summary(conn, _NOW)

    assert summary.date == "2026-05-22"
    assert summary.bot_health.status == "HEALTHY"
    assert summary.bot_health.heartbeat_fresh is True
    assert summary.equity.current_gbp == 1012.50
    assert summary.equity.change_24h_gbp == 12.50
    assert len(summary.strategies) == 1
    assert summary.strategies[0].status == "EVALUATING"
    assert summary.strategies[0].bars_have == 300


def test_build_daily_summary_bot_down(conn: psycopg.Connection[object]) -> None:
    # Heartbeat 6h stale → DOWN, with a prominent BOT NOT RUNNING note.
    _seed_bot_run(conn, heartbeat=_NOW - timedelta(hours=6))

    summary = build_daily_summary(conn, _NOW)

    assert summary.bot_health.status == "DOWN"
    assert summary.bot_health.heartbeat_fresh is False
    assert any("BOT NOT RUNNING" in n for n in summary.notes)


def test_build_daily_summary_zero_strategies(conn: psycopg.Connection[object]) -> None:
    # No strategies, no snapshots — must render a valid, empty summary.
    _seed_bot_run(conn, heartbeat=_NOW - timedelta(seconds=5))

    summary = build_daily_summary(conn, _NOW)

    assert summary.strategies == []
    assert summary.equity.current_gbp is None
    assert summary.risk_events_24h == 0


def test_warmup_strategy_detected(conn: psycopg.Connection[object]) -> None:
    _seed_bot_run(conn, heartbeat=_NOW - timedelta(seconds=5))
    _seed_strategy(conn, "Fresh strategy 4H BTC")
    _seed_candles(conn, 40)  # << ma_trend min_bars_needed → WARMUP

    summary = build_daily_summary(conn, _NOW)

    assert summary.strategies[0].status == "WARMUP"
    assert any("warmup" in n.lower() for n in summary.notes)


def test_generate_and_write(
    conn: psycopg.Connection[object], database_url: str, tmp_path: object,
) -> None:
    from pathlib import Path

    _seed_bot_run(conn, heartbeat=_NOW - timedelta(seconds=5))
    _seed_strategy(conn, "Golden Cross 4H BTC")
    _seed_candles(conn, 300)

    out = Path(str(tmp_path))
    summary, json_path, txt_path = generate_and_write(database_url, _NOW, out=out)

    assert json_path.name == "daily-summary-2026-05-22.json"
    assert txt_path.name == "daily-summary-2026-05-22.txt"
    assert json_path.is_file() and txt_path.is_file()
    # The JSON file is the source of truth — it reloads as the same summary.
    reloaded = DailySummary.model_validate_json(json_path.read_text(encoding="utf-8"))
    assert reloaded == summary
    assert "MarketMind Daily Summary" in txt_path.read_text(encoding="utf-8")
