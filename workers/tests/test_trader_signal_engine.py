"""Integration tests for the trader v1 signal engine.

Every test uses a real PostgresContainer (testcontainers) because
`evaluate_one_cycle` is fundamentally a DB-orchestration function —
unit-testing it without a DB would require mocking ~10 helpers and
exercise nothing real. All tests carry `@pytest.mark.integration`
and run only via `pytest -m integration`.

Seeding helpers (`_seed_strategy_version`, `_seed_candles_for_buy`,
`_seed_candles_flat`, `_seed_open_position`) live in this file —
the schema is stable enough that a shared conftest fixture would
add indirection without saving meaningful code.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from marketmind_shared.schemas.trader import RegimeState, StrategyState
from marketmind_workers.trader.config import TraderSettings, get_trader_settings
from marketmind_workers.trader.signal_engine import (
    _persist_strategy_state,  # pyright: ignore[reportPrivateUsage]
    evaluate_one_cycle,
)
from marketmind_workers.trader.templates.spec_template import SpecTemplate
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.integration


# ---- Fixtures --------------------------------------------------------------


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
def _clean_trader_tables(database_url: str) -> None:
    """Reset state between tests. The cascading FK chain means
    truncating strategies wipes versions, signals, orders, fills,
    positions, drift metrics, and risk events. Audit logs + alerts
    + candles are independent and need their own TRUNCATE.
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE trader_strategies, trader_candles, trader_audit_logs, "
            "trader_alerts RESTART IDENTITY CASCADE",
        )
        conn.commit()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TRADER_SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("TRADER_TIMEFRAMES", "4h")
    get_trader_settings.cache_clear()
    return get_trader_settings()


# ---- Seed helpers ----------------------------------------------------------


def _seed_strategy_version(
    database_url: str,
    *,
    template: str = "ma_trend",
    parameters: dict[str, Any] | None = None,
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
    enabled: bool = True,
    approved_for_paper: bool = True,
    backtest_metrics: dict[str, Any] | None = None,
) -> UUID:
    """Insert a strategy + one version. Returns the version's UUID."""
    name = f"test-strategy-{uuid4().hex[:8]}"
    params = parameters if parameters is not None else {}
    syms = symbols if symbols is not None else ["BTC/USDT"]
    tfs = timeframes if timeframes is not None else ["4h"]
    bt = backtest_metrics if backtest_metrics is not None else {}

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_strategies (name) VALUES (%s) RETURNING id",
            (name,),
        )
        strategy_row = cur.fetchone()
        assert strategy_row is not None
        strategy_id = strategy_row[0]
        cur.execute(
            """
            INSERT INTO trader_strategy_versions (
                strategy_id, version, marketmind_spec_id, template, parameters,
                symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                backtest_metrics, approved_for_paper, enabled
            ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                str(strategy_id),
                str(uuid4()),
                template,
                Jsonb(params),
                syms,
                tfs,
                Decimal("0.005"),
                Decimal("10"),
                Decimal("10"),
                Jsonb(bt),
                approved_for_paper,
                enabled,
            ),
        )
        version_row = cur.fetchone()
        assert version_row is not None
        conn.commit()
    return UUID(str(version_row[0]))


def _seed_candles(
    database_url: str,
    *,
    symbol: str,
    timeframe: str,
    closes: list[float],
    end_ts: datetime,
) -> None:
    """Insert N closed 4h candles ending at `end_ts`.

    Bar `i` opens at `end_ts - (N - i) * 4h`. Open = previous close
    (bar 0 opens at its own close). High = max(open, close) * 1.001;
    low = min(open, close) * 0.999. Matches the conftest
    `make_candles` shape so template tests and signal-engine
    integration tests see identical price geometry.
    """
    tf_seconds = 4 * 3600  # only 4h supported by these tests
    assert timeframe == "4h", "test seed helper supports 4h only"
    n = len(closes)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        for i, close_price in enumerate(closes):
            open_ts = end_ts - timedelta(seconds=tf_seconds * (n - i))
            close_ts = open_ts + timedelta(seconds=tf_seconds)
            prev_close = closes[i - 1] if i > 0 else close_price
            open_price = prev_close
            high = max(open_price, close_price) * 1.001
            low = min(open_price, close_price) * 0.999
            cur.execute(
                """
                INSERT INTO trader_candles
                    (symbol, timeframe, open_ts, close_ts,
                     open, high, low, close, volume, is_closed, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    symbol,
                    timeframe,
                    open_ts,
                    close_ts,
                    Decimal(str(open_price)),
                    Decimal(str(high)),
                    Decimal(str(low)),
                    Decimal(str(close_price)),
                    Decimal("1000"),
                    True,
                    "test",
                ),
            )
        conn.commit()


def _seed_open_position(
    database_url: str,
    *,
    version_id: UUID,
    symbol: str,
    entry_price: Decimal,
    stop_price: Decimal,
) -> UUID:
    """Seed an OPEN paper position. Returns the position UUID.

    A minimal phantom entry order is also inserted because
    `entry_order_id` is NOT NULL with an FK to trader_paper_orders.
    """
    order_id = uuid4()
    signal_id = uuid4()
    position_id = uuid4()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        # Phantom signal row to satisfy the order's FK.
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe,
                 candle_close_ts, signal, reason, indicators,
                 proposed_entry_price, proposed_stop_price)
            VALUES (%s, %s, %s, %s, %s, 'BUY', 'seed', %s, %s, %s)
            """,
            (
                str(signal_id),
                str(version_id),
                symbol,
                "4h",
                datetime(2026, 5, 1, tzinfo=UTC),
                Jsonb({}),
                entry_price,
                stop_price,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side,
                 order_type, requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, %s, 'BUY', 'MARKET', %s, 'FILLED', %s)
            """,
            (
                str(order_id),
                str(signal_id),
                str(version_id),
                symbol,
                Decimal("0.1"),
                datetime(2026, 5, 1, 4, tzinfo=UTC),
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_positions
                (id, strategy_version_id, symbol, side, entry_order_id,
                 entry_price, entry_ts, size, stop_price, status)
            VALUES (%s, %s, %s, 'LONG', %s, %s, %s, %s, %s, 'OPEN')
            """,
            (
                str(position_id),
                str(version_id),
                symbol,
                str(order_id),
                entry_price,
                datetime(2026, 5, 1, 4, tzinfo=UTC),
                Decimal("0.1"),
                stop_price,
            ),
        )
        conn.commit()
    return position_id


# ---- Tests -----------------------------------------------------------------


def test_cycle_persists_buy_signal_when_ma_trend_template_fires(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """20 flat bars + 1 climbing bar triggers ma_trend BUY at the
    last bar — matches the BUY scenario in
    `test_trader_template_ma_trend.test_evaluate_buys_on_clean_cross_with_trend_up`.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    # End the series at a candle close that's well before `now`
    # so it's recognised as closed. The signal engine doesn't take
    # `now` itself; it reads is_closed=TRUE candles regardless.
    end_ts = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    closes = [100.0] * 20 + [105.0]
    _seed_candles(database_url, symbol="BTC/USDT", timeframe="4h", closes=closes, end_ts=end_ts)

    result = evaluate_one_cycle(database_url, settings)

    assert result.versions_loaded == 1
    assert result.signals_persisted == 1
    assert result.holds == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT signal, proposed_entry_price FROM trader_signals "
            "WHERE strategy_version_id = %s",
            (str(version_id),),
        )
        row = cur.fetchone()
        assert row is not None
        kind, entry_price = row
        assert kind == "BUY"
        assert entry_price == Decimal("105")


def test_cycle_audits_hold_with_no_signal_row(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """Flat constant prices ⇒ HOLD. Audit row written; no signal row."""
    version_id = _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 30,
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )

    result = evaluate_one_cycle(database_url, settings)

    assert result.holds == 1
    assert result.signals_persisted == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_signals WHERE strategy_version_id = %s", (str(version_id),)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0

        cur.execute(
            "SELECT event, entity_id FROM trader_audit_logs WHERE actor = 'signal_engine'",
        )
        audit_rows = cur.fetchall()
        assert len(audit_rows) == 1
        assert audit_rows[0][0] == "hold_decision"
        assert audit_rows[0][1] == str(version_id)


def test_cycle_skips_disabled_versions(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
        enabled=False,
    )
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 20 + [105.0],
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )
    result = evaluate_one_cycle(database_url, settings)
    assert result.versions_loaded == 0
    assert result.signals_persisted == 0


def test_cycle_skips_unapproved_versions(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
        approved_for_paper=False,
    )
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 20 + [105.0],
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )
    result = evaluate_one_cycle(database_url, settings)
    assert result.versions_loaded == 0


def test_cycle_dedupes_repeated_runs_on_same_candle(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """Run twice — second cycle short-circuits on the dedupe check.
    Only one signal row in the DB.
    """
    _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 20 + [105.0],
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )
    first = evaluate_one_cycle(database_url, settings)
    second = evaluate_one_cycle(database_url, settings)

    assert first.signals_persisted == 1
    assert second.signals_persisted == 0
    assert second.pair_duplicate_signal == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_signals")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_cycle_skips_misconfigured_symbol(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """Version on ETH/USDT but env has only BTC/USDT.
    Intersection is empty ⇒ versions_misconfigured stat bumps,
    no eval attempted.
    """
    _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
        symbols=["ETH/USDT"],
    )
    result = evaluate_one_cycle(database_url, settings)
    assert result.versions_loaded == 1
    assert result.versions_misconfigured == 1
    assert result.evaluations == 0


def test_cycle_handles_insufficient_history(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """Only 5 candles — well below min_bars_needed for the
    default ma_trend params (=15). Pair attempt counts but no
    evaluation runs.
    """
    _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 5,
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )
    result = evaluate_one_cycle(database_url, settings)
    assert result.pair_attempts == 1
    assert result.pair_insufficient_history == 1
    assert result.evaluations == 0


def test_cycle_persists_exit_signal_when_position_open(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """ma_trend opposite-cross with an open position ⇒ EXIT signal.
    Same series shape as `test_trader_template_ma_trend.test_evaluate_exits_on_opposite_cross_with_open_position`.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    _seed_open_position(
        database_url,
        version_id=version_id,
        symbol="BTC/USDT",
        entry_price=Decimal("125"),
        stop_price=Decimal("115"),
    )
    closes = [100.0] * 20 + [105.0, 110.0, 115.0, 120.0, 125.0] + [120.0, 110.0]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )

    result = evaluate_one_cycle(database_url, settings)

    assert result.signals_persisted == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT signal, proposed_stop_price FROM trader_signals "
            "WHERE strategy_version_id = %s AND candle_close_ts = "
            "(SELECT MAX(candle_close_ts) FROM trader_signals WHERE strategy_version_id = %s)",
            (str(version_id), str(version_id)),
        )
        row = cur.fetchone()
        assert row is not None
        kind, stop_price = row
        assert kind == "EXIT"
        # EXIT carries the position's stop forward for the audit trail.
        assert stop_price == Decimal("115")


def test_cycle_respects_advisory_lock(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """Hold the advisory lock on a separate connection while the
    signal engine runs. The engine's per-pair lock attempt returns
    False; pair_locked_out increments; no signal is persisted.
    """
    from marketmind_shared.schemas.trader import LoopName
    from marketmind_workers.trader.locks import (
        try_advisory_xact_lock,
    )

    version_id = _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 20 + [105.0],
        end_ts=datetime(2026, 5, 17, tzinfo=UTC),
    )

    # Hold the lock on conn_holder; run evaluate_one_cycle (which
    # opens its own connection) while the lock is still held.
    with psycopg.connect(database_url) as conn_holder, conn_holder.transaction():
        assert try_advisory_xact_lock(conn_holder, LoopName.SIGNAL_EXECUTION, version_id)
        result = evaluate_one_cycle(database_url, settings)

    assert result.pair_attempts == 1
    assert result.pair_locked_out == 1
    assert result.signals_persisted == 0


# ---- Determinism (load-bearing invariant) ----------------------------------
#
# The trader's most important property: signal output depends ONLY
# on DB state (versions, candles, positions) and the strategy spec
# — NEVER on the wall clock. Two runs with the same DB state must
# produce byte-identical signal payloads regardless of when the
# cycle fires.
#
# These two tests guard the invariant directly: same seed, two
# different `now` values, signal/audit-log content identical. Only
# DB-clock-driven timestamps (the `created_at` column on
# trader_signals, `ts` on trader_audit_logs) are allowed to differ
# — those are set via SQL `DEFAULT NOW()`, outside the function's
# control.


def test_evaluate_one_cycle_signal_row_deterministic_across_now(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """BUY-path determinism. Run twice with `now` values 1h apart;
    the persisted signal row's content must be byte-identical
    (signal kind, reason, indicators JSON, all three Decimal price
    fields, candle_close_ts).

    The `ts`/`created_at` column is excluded from the SELECT because
    it's set by SQL `DEFAULT NOW()` and IS allowed to differ — the
    invariant under test is the SIGNAL PAYLOAD, not row-creation
    timestamps.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    end_ts = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 20 + [105.0],
        end_ts=end_ts,
    )

    t1 = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)

    def _read_signal_row() -> tuple[Any, ...]:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT signal, reason, indicators,
                       proposed_entry_price, proposed_stop_price,
                       proposed_take_profit_price, candle_close_ts
                FROM trader_signals
                WHERE strategy_version_id = %s
                """,
                (str(version_id),),
            )
            rows = cur.fetchall()
        assert len(rows) == 1, f"expected exactly one signal row, got {len(rows)}"
        return rows[0]

    # --- First run.
    result_1 = evaluate_one_cycle(database_url, settings, now=t1)
    assert result_1.signals_persisted == 1
    first_row = _read_signal_row()

    # Clear ONLY the output tables so the orchestrator's inputs
    # (versions, candles, positions) are unchanged between runs.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE trader_signals, trader_audit_logs RESTART IDENTITY CASCADE")
        conn.commit()

    # --- Second run with `now` 1 hour later.
    result_2 = evaluate_one_cycle(database_url, settings, now=t2)
    assert result_2.signals_persisted == 1
    second_row = _read_signal_row()

    assert second_row == first_row, (
        "signal row differs between runs — the orchestrator or template "
        "read the clock for decision logic, breaking determinism"
    )


def test_evaluate_one_cycle_hold_audit_payload_deterministic_across_now(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """HOLD-path determinism. The audit-log PAYLOAD jsonb (reason +
    indicators + candle_close_ts) must be byte-identical across
    runs; only the `ts` column may differ (DB DEFAULT NOW()).

    Mirror of the BUY test: same seed, two `now` values, identical
    output content.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={
            "fast_ema_period": 2,
            "slow_ema_period": 4,
            "trend_ema_period": 10,
            "atr_period": 5,
            "atr_mult": "2.0",
        },
    )
    end_ts = datetime(2026, 5, 17, 0, 0, tzinfo=UTC)
    # Flat constant prices ⇒ HOLD evaluation.
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[100.0] * 30,
        end_ts=end_ts,
    )

    t1 = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    t2 = t1 + timedelta(hours=1)

    def _read_audit_payload() -> dict[str, Any]:
        with psycopg.connect(database_url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT payload FROM trader_audit_logs
                WHERE actor = 'signal_engine' AND entity_id = %s
                """,
                (str(version_id),),
            )
            rows = cur.fetchall()
        assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
        return rows[0][0]

    # --- First run.
    result_1 = evaluate_one_cycle(database_url, settings, now=t1)
    assert result_1.holds == 1
    first_payload = _read_audit_payload()

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE trader_audit_logs RESTART IDENTITY")
        conn.commit()

    # --- Second run with `now` 1 hour later.
    result_2 = evaluate_one_cycle(database_url, settings, now=t2)
    assert result_2.holds == 1
    second_payload = _read_audit_payload()

    assert second_payload == first_payload, (
        "audit-log payload differs between runs — the HOLD-path read the "
        "clock for content, breaking determinism"
    )


# ---- A.5b: stateful state persistence + the idempotency guard --------------

_SUPERTREND_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "strategies"
    / "valid"
    / "09_regime_state_supertrend.json"
)


def _supertrend_spec_params() -> dict[str, Any]:
    """The Supertrend regime fixture wrapped as `spec`-template params."""
    return {"spec": json.loads(_SUPERTREND_FIXTURE.read_text())}


def test_stateful_spec_advances_state_exactly_once_per_candle(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """The idempotency guard (design doc §6A.2): the 1-minute tick
    re-evaluates the same closed candle many times; the stateful state
    must advance exactly once. Ten cycles on one latest candle leave
    exactly one trader_strategy_state row, and the candle is evaluated
    exactly once.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="spec",
        parameters=_supertrend_spec_params(),
    )
    # 1100 4h candles — clears the SpecTemplate's 5x-warmup window for
    # the Supertrend EMA(200) spec (min_bars_needed ~1005).
    closes = [100.0 + i * 0.2 for i in range(1100)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    results = [evaluate_one_cycle(database_url, settings) for _ in range(10)]

    # Cycle 1 evaluates the candle once; cycles 2-10 must not re-evaluate
    # it — the state guard (HOLD) or the trader_signals dedupe (a fired
    # signal) skips them before evaluation.
    assert results[0].evaluations == 1
    assert all(r.evaluations == 0 for r in results[1:])

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_strategy_state WHERE strategy_version_id = %s",
            (str(version_id),),
        )
        state_row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM trader_signals WHERE strategy_version_id = %s",
            (str(version_id),),
        )
        signal_row = cur.fetchone()
    assert state_row is not None
    assert signal_row is not None
    assert state_row[0] == 1, (
        f"idempotency guard failed: {state_row[0]} trader_strategy_state rows "
        "after 10 cycles on the same candle — expected exactly 1"
    )
    assert signal_row[0] <= 1


def test_strategy_state_insert_is_idempotent_on_conflict(
    database_url: str,
    _clean_trader_tables: None,
) -> None:
    """The trader_strategy_state UNIQUE (version, symbol, timeframe,
    candle_close_ts) + ON CONFLICT DO NOTHING is the cross-worker net
    (design doc §6A.2): two writers advancing the same candle past the
    advisory lock leave exactly one row — the first write stands.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="spec",
        parameters=_supertrend_spec_params(),
    )
    candle_ts = datetime(2024, 6, 1, tzinfo=UTC)
    state_a = StrategyState(regimes=[RegimeState(latched=True)])
    state_b = StrategyState(regimes=[RegimeState(latched=False)])

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            _persist_strategy_state(conn, version_id, "BTC/USDT", "4h", candle_ts, state_a)
        with conn.transaction():
            # A second worker advancing the same candle — ON CONFLICT
            # DO NOTHING discards it.
            _persist_strategy_state(conn, version_id, "BTC/USDT", "4h", candle_ts, state_b)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM trader_strategy_state WHERE strategy_version_id = %s",
                (str(version_id),),
            )
            rows = cur.fetchall()

    assert len(rows) == 1, f"ON CONFLICT failed: {len(rows)} rows for one candle"
    assert StrategyState.model_validate(rows[0][0]).regimes[0].latched is True


def test_v1_strategy_version_writes_no_state_rows(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """A v1 (non-stateful) version never touches trader_strategy_state —
    the stateful path is gated on isinstance(SpecTemplate) + is_stateful,
    so v1 templates run exactly as before A.5b (design doc §6A.5).
    """
    _seed_strategy_version(
        database_url,
        template="ma_trend",
        parameters={"fast_ema_period": 12, "slow_ema_period": 26},
    )
    closes = [100.0 + i * 0.2 for i in range(300)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    evaluate_one_cycle(database_url, settings)

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_strategy_state")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == 0, "a v1 (non-stateful) template wrote a trader_strategy_state row"


@pytest.mark.parametrize(
    "corruption",
    [
        "SET state = '{\"garbage\": true}'::jsonb",
        "SET state_schema_version = 999",
    ],
    ids=["unparseable-jsonb", "unknown-schema-version"],
)
def test_corrupt_strategy_state_disables_version_and_alerts(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
    corruption: str,
) -> None:
    """A.5c §6A.3 disable-and-alert: a trader_strategy_state row that
    cannot be trusted — unparseable JSONB, or a state_schema_version this
    engine does not understand — disables the strategy version and writes
    a WARNING alert. The trader never trades on unknown state.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="spec",
        parameters=_supertrend_spec_params(),
    )
    closes = [100.0 + i * 0.2 for i in range(1101)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    first = evaluate_one_cycle(database_url, settings)
    assert first.evaluations == 1  # cycle 1 evaluated and wrote a state row
    assert first.holds == 1, "linear data must HOLD so cycle 2 reaches the state load"

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE trader_strategy_state {corruption}")
        conn.commit()

    second = evaluate_one_cycle(database_url, settings)
    assert second.pair_state_disabled == 1
    assert second.evaluations == 0, "corrupt state must skip evaluation — no trade"

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT enabled FROM trader_strategy_versions WHERE id = %s",
            (str(version_id),),
        )
        enabled_row = cur.fetchone()
        cur.execute(
            "SELECT severity FROM trader_alerts WHERE subject = %s",
            ("Strategy auto-disabled — corrupt stateful state",),
        )
        alert_rows = cur.fetchall()
    assert enabled_row is not None
    assert enabled_row[0] is False, "the version must be disabled"
    assert len(alert_rows) == 1, "exactly one disable alert must be written"
    assert alert_rows[0][0] == "warning"

    # The disabled version is no longer loaded — a third cycle is a no-op.
    third = evaluate_one_cycle(database_url, settings)
    assert third.versions_loaded == 0


def test_stateful_evaluation_exception_disables_version_without_crashing(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A.5c exception hardening: an exception raised inside
    evaluate_stateful is caught — the cycle completes (the trader does
    not crash), and the offending version is disabled + alerted, exactly
    like corrupt state (design doc §6A.3).
    """
    version_id = _seed_strategy_version(
        database_url,
        template="spec",
        parameters=_supertrend_spec_params(),
    )
    closes = [100.0 + i * 0.2 for i in range(1101)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    def _boom(
        _self: SpecTemplate,
        _candles: object,
        _position: object,
        _prior_state: object,
    ) -> object:
        raise RuntimeError("injected stateful-evaluation failure")

    monkeypatch.setattr(SpecTemplate, "evaluate_stateful", _boom)

    # Must NOT raise — if the exception escaped _evaluate_pair this line
    # would fail, which is itself the "trader doesn't crash" assertion.
    result = evaluate_one_cycle(database_url, settings)
    assert result.pair_state_disabled == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT enabled FROM trader_strategy_versions WHERE id = %s",
            (str(version_id),),
        )
        enabled_row = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM trader_alerts WHERE subject = %s",
            ("Strategy auto-disabled — corrupt stateful state",),
        )
        alert_row = cur.fetchone()
    assert enabled_row is not None
    assert enabled_row[0] is False
    assert alert_row is not None
    assert alert_row[0] == 1


def test_state_survives_restart_and_resumes_at_the_next_candle(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """A.5c recovery: a trader restart loses no state — state lives only
    in trader_strategy_state, never in process memory (§6A.3). After a
    cycle writes a state row, the restart's first cycle re-runs on the
    same candle and is a no-op via the idempotency guard; the next candle
    is then evaluated seeded from the persisted row. Each evaluate_one_cycle
    call opens its own connection, so successive calls *are* post-restart
    cycles by construction.
    """
    _seed_strategy_version(
        database_url,
        template="spec",
        parameters=_supertrend_spec_params(),
    )
    closes = [100.0 + i * 0.2 for i in range(1101)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    # Cycle A — evaluates candle C, writes a state row.
    cycle_a = evaluate_one_cycle(database_url, settings)
    assert cycle_a.evaluations == 1

    # Cycle B — the bot restarted; this cycle re-runs on the same candle C.
    # State is reloaded from the DB and the idempotency guard skips it.
    cycle_b = evaluate_one_cycle(database_url, settings)
    assert cycle_b.evaluations == 0
    assert cycle_b.pair_state_guarded == 1

    # A new candle C+1 closes, 4h after C.
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=[320.2],
        end_ts=datetime(2024, 6, 1, 4, 0, 0, tzinfo=UTC),
    )

    # Cycle C — post-restart, evaluates C+1 seeded from C's persisted row.
    cycle_c = evaluate_one_cycle(database_url, settings)
    assert cycle_c.evaluations == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT candle_close_ts FROM trader_strategy_state ORDER BY candle_close_ts",
        )
        rows = cur.fetchall()
    # Exactly two state rows — one per evaluated candle; cycle B added none.
    assert len(rows) == 2
    assert rows[0][0] < rows[1][0]


def test_tier3_spec_persists_tier3_state(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """A.6 end-to-end: a Tier-3 (Turtle System 1, prior_signal) version
    evaluates through the signal engine — full-history load, the
    iterative_live shadow stepper — and persists a trader_strategy_state
    row carrying the Tier3 checkpoint, stamped schema version 2.
    """
    turtle = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "tests" / "fixtures" / "strategies" / "valid" / "11_turtle_system1.json"
        ).read_text(),
    )
    version_id = _seed_strategy_version(
        database_url,
        template="spec",
        parameters={"spec": turtle},
    )
    closes = [100.0 + i * 0.2 for i in range(1100)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    result = evaluate_one_cycle(database_url, settings)
    assert result.evaluations == 1
    assert result.pair_state_disabled == 0, "the Tier-3 spec must not be disabled"

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state, state_schema_version FROM trader_strategy_state "
            "WHERE strategy_version_id = %s",
            (str(version_id),),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    state = StrategyState.model_validate(rows[0][0])
    assert state.tier3 is not None, "a Tier-3 spec must persist a tier3 block"
    assert state.tier3.last_bar == len(closes) - 1
    assert rows[0][1] == 2, "a tier3 state row is stamped state_schema_version 2"
    # Turtle prints breakout signals over 1100 bars — the checkpoint is real.
    assert len(state.tier3.signal_history) > 0


def test_v2_supertrend_smoke_through_signal_engine(
    database_url: str,
    settings: TraderSettings,
    _clean_trader_tables: None,
) -> None:
    """A.7 v2 end-to-end smoke: a v2 Tier-2 spec (Supertrend regime_state)
    seeded against SpecTemplate evaluates through the signal engine via
    the build_signals_stateful path, persists a schema-1 state row with
    NO tier3 block (the Tier-2 counterpart to the Tier-3 e2e test above),
    and the idempotency guard holds on a second cycle.
    """
    version_id = _seed_strategy_version(
        database_url,
        template="spec",
        parameters=_supertrend_spec_params(),
    )
    closes = [100.0 + i * 0.2 for i in range(1100)]
    _seed_candles(
        database_url,
        symbol="BTC/USDT",
        timeframe="4h",
        closes=closes,
        end_ts=datetime(2024, 6, 1, tzinfo=UTC),
    )

    first = evaluate_one_cycle(database_url, settings)
    assert first.evaluations == 1
    assert first.pair_state_disabled == 0, "the Tier-2 spec must not be disabled"

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state, state_schema_version FROM trader_strategy_state "
            "WHERE strategy_version_id = %s",
            (str(version_id),),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    state = StrategyState.model_validate(rows[0][0])
    assert state.tier3 is None, "a Tier-2 spec must not carry a tier3 block"
    assert rows[0][1] == 1, "a Tier-2 state row is stamped state_schema_version 1"

    # Idempotency guard: a second cycle on the same candle adds no state row.
    second = evaluate_one_cycle(database_url, settings)
    assert second.evaluations == 0
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_strategy_state WHERE strategy_version_id = %s",
            (str(version_id),),
        )
        count_row = cur.fetchone()
    assert count_row is not None
    assert count_row[0] == 1, "the idempotency guard must hold — exactly one row"
