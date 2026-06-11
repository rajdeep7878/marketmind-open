"""End-to-end runner integration test (Step 13 requirement).

Drives the trader's 6-phase main cycle through a real Postgres
testcontainer and fakeredis, with a synthetic candle stream
designed to trigger a BUY signal on a known bar. Verifies:

  1. First cycle: signal → risk-approved → PENDING order written.
     (The fill candle isn't in the DB yet, so the order stays
     PENDING — exactly the "mid-cycle interruption" scenario
     the user asked to simulate.)
  2. "Process kill" — close all connections, mark bot_run
     'crashed', create a new bot_run row (simulating a fresh
     runner boot via orphan cleanup).
  3. Add the fill candle.
  4. Second cycle: order fills, position opens, snapshot
     reflects the cash drawdown.
  5. Third cycle (no new candles): asserts NO duplicate signals,
     orders, fills, or positions.
  6. Portfolio cash math: ending cash = starting − notional − fee.

The exchange adapter is monkeypatched to a stub returning empty
OHLCV lists, so the ingestion phase becomes a no-op (we pre-seed
the candle table). The alerts dispatcher uses fakeredis-only
delivery (no Telegram creds, so it short-circuits via the
documented "no-creds → delivered=False" path).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fakeredis import FakeRedis
from marketmind_shared.schemas.trader import LoopName
from marketmind_workers.trader import alerts as alerts_module
from marketmind_workers.trader import drift as drift_module
from marketmind_workers.trader import execution as execution_module
from marketmind_workers.trader import heartbeat as heartbeat_module
from marketmind_workers.trader import ingestion as ingestion_module
from marketmind_workers.trader import jobs as jobs_module
from marketmind_workers.trader import risk as risk_module
from marketmind_workers.trader import signal_engine as signal_engine_module
from marketmind_workers.trader.config import get_trader_settings
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration


# ---- Test fixtures ---------------------------------------------------------


@pytest.fixture(scope="module")
def pg_container() -> Iterator[Any]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: Any) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def _clean(database_url: str) -> None:
    """Truncate every trader table that the test writes."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
                trader_portfolio_snapshots,
                trader_paper_fills,
                trader_paper_orders,
                trader_paper_positions,
                trader_signals,
                trader_candles,
                trader_strategy_versions,
                trader_strategies,
                trader_bot_runs,
                trader_alerts,
                trader_audit_logs,
                trader_risk_events,
                trader_drift_metrics
            RESTART IDENTITY CASCADE
            """,
        )
        conn.commit()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=False)


@pytest.fixture
def _trader_env(
    monkeypatch: pytest.MonkeyPatch,
    database_url: str,
    fake_redis: FakeRedis,
) -> None:
    """Point get_trader_settings() at the testcontainer DB; patch
    Redis access in jobs.py to use the fakeredis instance; patch
    the BinanceAdapter to a no-op stub.
    """
    # Settings env vars.
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:9999/0")  # never used
    monkeypatch.setenv("TRADER_ALLOW_LIVE", "false")
    monkeypatch.setenv("TRADER_QUEUE_NAME", "trader_default_e2e")
    monkeypatch.setenv("TRADER_SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("TRADER_TIMEFRAMES", "4h")
    monkeypatch.setenv("TRADER_STARTING_CASH_GBP", "10000")
    monkeypatch.setenv("TRADER_DEFAULT_FEE_BPS", "10")
    monkeypatch.setenv("TRADER_DEFAULT_SLIPPAGE_BPS", "10")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    get_trader_settings.cache_clear()

    # Route all Redis access in jobs.py to fakeredis.
    monkeypatch.setattr(jobs_module, "_connect_redis", lambda _settings: fake_redis)

    # No-op exchange adapter — the cycle's ingest phase becomes
    # idempotent because every fetch returns 0 candles.
    class _NoOpAdapter:
        def fetch_recent_ohlcv(
            self,
            _symbol: str,
            _timeframe: str,
            *,
            limit: int,
        ) -> list[list[float]]:
            _ = limit
            return []

        def fetch_ohlcv_since(
            self,
            _symbol: str,
            _timeframe: str,
            *,
            since_ms: int,
        ) -> list[list[float]]:
            _ = since_ms
            return []

    # Phase C C.1.4: ingestion now dispatches via `make_adapter` from
    # exchanges.py instead of constructing `BinanceAdapter()` directly.
    # The monkeypatch target moves to the factory so the e2e cycle's
    # ingest phase remains no-op'd.
    monkeypatch.setattr(ingestion_module, "make_adapter", lambda _ac: _NoOpAdapter())


def _freeze_now(monkeypatch: pytest.MonkeyPatch, frozen: datetime) -> None:
    """Pin `now_utc()` to `frozen` in every trader module that
    imports it. Each module has its own local reference (from
    `from marketmind_shared.trader.time import now_utc`), so a
    single global patch isn't sufficient.
    """
    for mod in (
        ingestion_module,
        signal_engine_module,
        risk_module,
        execution_module,
        drift_module,
        alerts_module,
    ):
        if hasattr(mod, "now_utc"):
            monkeypatch.setattr(mod, "now_utc", lambda f=frozen: f)


# ---- Seed helpers ----------------------------------------------------------


_FOUR_HOURS: timedelta = timedelta(hours=4)
# End of the seeded candle series — the BUY trigger bar's close_ts.
_BUY_SIGNAL_TS: datetime = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
# The fill candle opens at the signal's candle_close_ts and closes 4h later.
_FILL_CANDLE_OPEN_TS: datetime = _BUY_SIGNAL_TS
_FILL_CANDLE_CLOSE_TS: datetime = _BUY_SIGNAL_TS + _FOUR_HOURS
# Frozen "now" each phase function sees — 1 minute after the signal
# bar closes. Stays inside the trader_data_staleness_seconds window
# (default 600s) so risk.py doesn't block the BUY on STALE_DATA.
_FROZEN_NOW: datetime = _BUY_SIGNAL_TS + timedelta(minutes=1)
# After the fill candle closes, the cycle's "now" advances. We pick
# a moment 1 minute after the fill candle closes so the execution
# phase sees the candle as available and the staleness check passes.
_FROZEN_NOW_POST_FILL: datetime = _FILL_CANDLE_CLOSE_TS + timedelta(minutes=1)

_VALID_BACKTEST_METRICS: dict[str, Any] = {
    "walk_forward": {"out_of_sample_trade_freq_per_week": 3.5},
    "single_pass": {
        "win_rate": 0.55,
        "avg_return_per_trade": 0.012,
        "max_drawdown_pct": 0.08,
    },
}


def _seed_strategy_version(database_url: str) -> UUID:
    """Insert a strategy + approved version with ma_trend tuned
    for a small-bar BUY trigger.

    Mirrors `test_trader_signal_engine.py`'s canonical
    `(fast=2, slow=4, trend=10, atr=5)` config that fires on
    `closes = [100]*20 + [105]`.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_strategies (name) VALUES (%s) RETURNING id",
            (f"e2e-strategy-{uuid4().hex[:8]}",),
        )
        srow = cur.fetchone()
        assert srow is not None
        cur.execute(
            """
            INSERT INTO trader_strategy_versions (
                strategy_id, version, marketmind_spec_id, template, parameters,
                symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                backtest_metrics, approved_for_paper, enabled
            ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE)
            RETURNING id
            """,
            (
                str(srow[0]),
                str(uuid4()),
                "ma_trend",
                Jsonb(
                    {
                        "fast_ema_period": 2,
                        "slow_ema_period": 4,
                        "trend_ema_period": 10,
                        "atr_period": 5,
                        "atr_mult": "2.0",
                    },
                ),
                ["BTC/USDT"],
                ["4h"],
                Decimal("0.005"),
                10,
                10,
                Jsonb(_VALID_BACKTEST_METRICS),
            ),
        )
        vrow = cur.fetchone()
        assert vrow is not None
        conn.commit()
    return UUID(str(vrow[0]))


def _seed_buy_trigger_candles(database_url: str) -> None:
    """20 flat bars at 100 + one bar at 105. The cross fires on the last bar."""
    closes = [100.0] * 20 + [105.0]
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        for i, close in enumerate(closes):
            open_ts = _BUY_SIGNAL_TS - _FOUR_HOURS * (len(closes) - i)
            close_ts = open_ts + _FOUR_HOURS
            prev_close = closes[i - 1] if i > 0 else close
            cur.execute(
                """
                INSERT INTO trader_candles
                    (symbol, timeframe, open_ts, close_ts,
                     open, high, low, close, volume, is_closed, source)
                VALUES ('BTC/USDT', '4h', %s, %s, %s, %s, %s, %s, 1000, TRUE, 'test')
                """,
                (
                    open_ts,
                    close_ts,
                    Decimal(str(prev_close)),
                    Decimal(str(max(prev_close, close) * 1.001)),
                    Decimal(str(min(prev_close, close) * 0.999)),
                    Decimal(str(close)),
                ),
            )
        conn.commit()


def _seed_fill_candle(database_url: str, *, open_price: Decimal) -> None:
    """The candle whose open_ts matches the order's intended_fill_ts."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_candles
                (symbol, timeframe, open_ts, close_ts,
                 open, high, low, close, volume, is_closed, source)
            VALUES ('BTC/USDT', '4h', %s, %s, %s, %s, %s, %s, 1000, TRUE, 'test')
            """,
            (
                _FILL_CANDLE_OPEN_TS,
                _FILL_CANDLE_CLOSE_TS,
                open_price,
                open_price * Decimal("1.005"),
                open_price * Decimal("0.995"),
                open_price * Decimal("1.002"),
            ),
        )
        conn.commit()


def _create_run(database_url: str, *, worker_id: str) -> UUID:
    with psycopg.connect(database_url) as conn, conn.transaction():
        return heartbeat_module.create_bot_run(
            conn,
            loop_name=LoopName.RUNNER,
            worker_id=worker_id,
        )


def _count(database_url: str, table: str) -> int:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _fetch_one(database_url: str, query: str) -> Any:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(query)
        return cur.fetchone()


# ---- The test --------------------------------------------------------------


def test_end_to_end_runner_with_crash_and_restart(
    database_url: str,
    _clean: None,
    _trader_env: None,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full end-to-end scenario described at the top of this
    file.

    Sections:
      A. Seed strategy + 21 candles → expect BUY trigger on bar 21.
      B. Create initial bot_run; run tick_main_cycle once.
         Verify: 1 signal, 1 PENDING order, 0 fills, 0 positions.
      C. Simulate process kill: mark bot_run crashed, create new
         one (orphan cleanup, same as runner.main() does at boot).
      D. Add fill candle. Run tick_main_cycle again.
         Verify: 1 signal (same), 1 FILLED order (same), 1 fill,
         1 OPEN position, 2 snapshots, cash math correct.
      E. Run tick_main_cycle a third time on the same data.
         Verify: no duplicate inserts in any table.
    """
    # ---- Section A: seed ---------------------------------------------------
    version_id = _seed_strategy_version(database_url)
    _seed_buy_trigger_candles(database_url)

    initial_run_id = _create_run(database_url, worker_id="runner-1")
    assert _count(database_url, "trader_bot_runs") == 1

    # ---- Section B: first cycle (signal + risk approval, no fill yet) ----
    # Pin "now" to just after the BUY-trigger bar closes so the
    # staleness check in risk.py passes.
    _freeze_now(monkeypatch, _FROZEN_NOW)
    jobs_module.tick_main_cycle()

    # Signal written.
    assert _count(database_url, "trader_signals") == 1
    sig_row = _fetch_one(
        database_url,
        "SELECT signal, strategy_version_id FROM trader_signals",
    )
    assert sig_row is not None
    assert sig_row[0] == "BUY"
    assert UUID(str(sig_row[1])) == version_id

    # PENDING order written (no fill candle yet).
    assert _count(database_url, "trader_paper_orders") == 1
    ord_row = _fetch_one(
        database_url,
        "SELECT side, status, intended_fill_ts FROM trader_paper_orders",
    )
    assert ord_row is not None
    assert ord_row[0] == "BUY"
    assert ord_row[1] == "PENDING"
    assert ord_row[2] == _BUY_SIGNAL_TS

    # No fill / no position yet.
    assert _count(database_url, "trader_paper_fills") == 0
    assert _count(database_url, "trader_paper_positions") == 0
    # First snapshot recorded.
    assert _count(database_url, "trader_portfolio_snapshots") == 1

    # ---- Section C: simulate process kill ---------------------------------
    # Mark the old run crashed (mimicking a kill -9 followed by a
    # new runner boot's orphan cleanup), then create a fresh one.
    with psycopg.connect(database_url) as conn, conn.transaction():
        heartbeat_module.mark_crashed(conn, initial_run_id, reason="simulated kill")
    new_run_id = _create_run(database_url, worker_id="runner-2")
    orphaned = jobs_module.mark_orphaned_runs_crashed(database_url, new_run_id)
    # The initial run was already manually crashed; orphan-cleanup
    # finds zero remaining 'running' rows to transition.
    assert orphaned == 0
    assert new_run_id != initial_run_id

    # ---- Section D: add fill candle + second cycle ------------------------
    fill_open_price = Decimal("105")
    _seed_fill_candle(database_url, open_price=fill_open_price)

    # Advance the frozen clock to just after the fill candle's close
    # so risk's staleness check still passes for any subsequent
    # signal evaluation against the latest closed bar.
    _freeze_now(monkeypatch, _FROZEN_NOW_POST_FILL)
    jobs_module.tick_main_cycle()

    # No new signal (the signal_engine evaluates on the latest
    # closed candle — now the fill candle — and the fast/slow
    # haven't crossed there).
    assert _count(database_url, "trader_signals") == 1
    # The order is now FILLED.
    assert _count(database_url, "trader_paper_orders") == 1
    ord_status = _fetch_one(database_url, "SELECT status FROM trader_paper_orders")
    assert ord_status is not None
    assert ord_status[0] == "FILLED"
    # Exactly one fill.
    assert _count(database_url, "trader_paper_fills") == 1
    # One OPEN position.
    assert _count(database_url, "trader_paper_positions") == 1
    pos_row = _fetch_one(
        database_url,
        "SELECT status, side, entry_price FROM trader_paper_positions",
    )
    assert pos_row is not None
    assert pos_row[0] == "OPEN"
    assert pos_row[1] == "LONG"
    # entry_price = fill_open_price * (1 + slippage/10000) = 105 * 1.001 = 105.105
    expected_entry = fill_open_price * Decimal("1.001")
    assert pos_row[2] == expected_entry, f"entry_price={pos_row[2]} vs expected={expected_entry}"
    # Snapshot count is 2 (one per cycle).
    assert _count(database_url, "trader_portfolio_snapshots") == 2

    # ---- Portfolio cash math ----------------------------------------------
    fill_row = _fetch_one(
        database_url,
        "SELECT fill_price, size, fee FROM trader_paper_fills",
    )
    assert fill_row is not None
    fill_price, size, fee = fill_row
    notional = fill_price * size
    expected_cash = Decimal("10000") - notional - fee

    snap_row = _fetch_one(
        database_url,
        "SELECT cash, equity, open_positions_count "
        "FROM trader_portfolio_snapshots ORDER BY ts DESC LIMIT 1",
    )
    assert snap_row is not None
    cash, equity, open_count = snap_row
    # Postgres NUMERIC rounds at column scale; compare within one
    # cent rather than insisting on identical full-precision
    # Decimals. Trader money math is exact at the GBP-cent level
    # which is what the operator cares about.
    cent_tolerance = Decimal("0.01")
    assert abs(cash - expected_cash) < cent_tolerance, (
        f"cash math: got {cash}, expected {expected_cash} (notional={notional}, fee={fee})"
    )
    assert open_count == 1
    # Equity = cash + MTM. MTM = size * latest_close. Latest close
    # is the fill candle's close (open_price * 1.002).
    latest_close = fill_open_price * Decimal("1.002")
    expected_equity = cash + size * latest_close
    assert abs(equity - expected_equity) < cent_tolerance, (
        f"equity={equity}, expected={expected_equity}"
    )

    # ---- Section E: third cycle (no new data) — no duplicates ------------
    jobs_module.tick_main_cycle()

    assert _count(database_url, "trader_signals") == 1, "signal duplicated"
    assert _count(database_url, "trader_paper_orders") == 1, "order duplicated"
    assert _count(database_url, "trader_paper_fills") == 1, "fill duplicated"
    assert _count(database_url, "trader_paper_positions") == 1, "position duplicated"
    # Snapshot grows monotonically — that's expected; one per cycle.
    assert _count(database_url, "trader_portfolio_snapshots") == 3

    # Re-enqueue inserted three jobs into fakeredis (one per cycle).
    # The exact count isn't load-bearing for this test; we just
    # confirm the cycle finished without raising.

    # Sanity: no critical phase-failure alerts were emitted, which
    # would indicate a phase blew up during the test.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_alerts "
            "WHERE severity = 'critical' AND subject LIKE 'Trader phase failing%'",
        )
        crit_row = cur.fetchone()
    assert crit_row is not None
    assert crit_row[0] == 0, "phase-failure alerts indicate a bug in the cycle"
