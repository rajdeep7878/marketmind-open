"""Tests for the trader v1 portfolio manager.

Layer 1: PURE unit tests for `_aggregate_breakdowns` (no DB).
Layer 2: integration tests for `compute_and_persist_snapshot` +
the read helpers via testcontainers. Mark integration.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from marketmind_workers.trader.config import TraderSettings, get_trader_settings
from marketmind_workers.trader.portfolio import (
    _aggregate_breakdowns,
    _BreakdownEntry,
    _ClosedPositionRow,
    _OpenPositionRow,
    compute_and_persist_snapshot,
    fetch_equity_curve,
    fetch_latest_snapshot,
)
from psycopg.types.json import Jsonb

# ---- Layer 1: pure unit tests for _aggregate_breakdowns -------------------


class TestAggregateBreakdowns:
    """`_aggregate_breakdowns` is pure: list of open + list of
    closed + latest-close dict → per-strategy + per-symbol dicts.
    These tests pin the aggregation semantics in isolation.
    """

    def test_empty_inputs_produce_empty_dicts(self) -> None:
        per_strategy, per_symbol = _aggregate_breakdowns([], [], {})
        assert per_strategy == {}
        assert per_symbol == {}

    def test_one_open_position_contributes_unrealised(self) -> None:
        v_id = uuid4()
        pos = _OpenPositionRow(
            position_id=uuid4(),
            strategy_version_id=v_id,
            symbol="BTC/USDT",
            size=Decimal("2"),
            entry_price=Decimal("100"),
        )
        latest = {"BTC/USDT": Decimal("110")}
        per_strategy, per_symbol = _aggregate_breakdowns([pos], [], latest)
        # unrealised = (110 - 100) * 2 = 20
        assert per_strategy[str(v_id)].unrealised_pnl == Decimal("20")
        assert per_strategy[str(v_id)].realised_pnl == Decimal("0")
        assert per_strategy[str(v_id)].open_positions == 1
        assert per_symbol["BTC/USDT"].unrealised_pnl == Decimal("20")

    def test_open_position_without_latest_close_uses_entry_price(self) -> None:
        """When no candle exists for the symbol, MTM falls back to
        entry_price → zero unrealised PnL.
        """
        pos = _OpenPositionRow(
            position_id=uuid4(),
            strategy_version_id=uuid4(),
            symbol="UNKNOWN/USDT",
            size=Decimal("1"),
            entry_price=Decimal("50"),
        )
        _, per_symbol = _aggregate_breakdowns([pos], [], {})
        assert per_symbol["UNKNOWN/USDT"].unrealised_pnl == Decimal("0")

    def test_closed_position_contributes_realised(self) -> None:
        v_id = uuid4()
        closed = _ClosedPositionRow(
            strategy_version_id=v_id,
            symbol="BTC/USDT",
            realised_pnl=Decimal("9.50"),
        )
        per_strategy, per_symbol = _aggregate_breakdowns([], [closed], {})
        assert per_strategy[str(v_id)].realised_pnl == Decimal("9.50")
        assert per_strategy[str(v_id)].open_positions == 0
        assert per_symbol["BTC/USDT"].realised_pnl == Decimal("9.50")

    def test_strategy_with_open_and_closed_aggregates_both(self) -> None:
        v_id = uuid4()
        open_pos = _OpenPositionRow(
            position_id=uuid4(),
            strategy_version_id=v_id,
            symbol="BTC/USDT",
            size=Decimal("1"),
            entry_price=Decimal("100"),
        )
        closed_pos = _ClosedPositionRow(
            strategy_version_id=v_id,
            symbol="BTC/USDT",
            realised_pnl=Decimal("5"),
        )
        per_strategy, _ = _aggregate_breakdowns(
            [open_pos],
            [closed_pos],
            {"BTC/USDT": Decimal("110")},
        )
        entry = per_strategy[str(v_id)]
        assert entry.realised_pnl == Decimal("5")
        assert entry.unrealised_pnl == Decimal("10")
        assert entry.open_positions == 1

    def test_multiple_strategies_dont_bleed(self) -> None:
        v_a, v_b = uuid4(), uuid4()
        per_strategy, _ = _aggregate_breakdowns(
            [
                _OpenPositionRow(
                    position_id=uuid4(),
                    strategy_version_id=v_a,
                    symbol="BTC/USDT",
                    size=Decimal("1"),
                    entry_price=Decimal("100"),
                ),
                _OpenPositionRow(
                    position_id=uuid4(),
                    strategy_version_id=v_b,
                    symbol="BTC/USDT",
                    size=Decimal("2"),
                    entry_price=Decimal("100"),
                ),
            ],
            [],
            {"BTC/USDT": Decimal("110")},
        )
        assert per_strategy[str(v_a)].unrealised_pnl == Decimal("10")
        assert per_strategy[str(v_b)].unrealised_pnl == Decimal("20")

    def test_to_jsonable_serializes_decimals_as_strings(self) -> None:
        """JSONB round-trip safety: Decimals serialise as strings so
        a downstream `json.loads` doesn't see them as floats.
        """
        entry = _BreakdownEntry(
            realised_pnl=Decimal("12.34"),
            unrealised_pnl=Decimal("-5.67"),
            open_positions=2,
        )
        result = entry.to_jsonable()
        assert result["realised_pnl"] == "12.34"
        assert result["unrealised_pnl"] == "-5.67"
        assert result["open_positions"] == 2


# ---- Layer 2: integration tests --------------------------------------------


pytestmark_integration = pytest.mark.integration


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
def _clean(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE trader_strategies, trader_candles, "
            "trader_portfolio_snapshots, trader_alerts, trader_risk_events, "
            "trader_audit_logs RESTART IDENTITY CASCADE",
        )
        conn.commit()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TRADER_STARTING_CASH_GBP", "1000")
    get_trader_settings.cache_clear()
    return get_trader_settings()


# ---- Seed helpers ----------------------------------------------------------


def _seed_version(database_url: str) -> UUID:
    name = f"portfolio-test-{uuid4().hex[:8]}"
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_strategies (name) VALUES (%s) RETURNING id",
            (name,),
        )
        srow = cur.fetchone()
        assert srow is not None
        sid = srow[0]
        cur.execute(
            """
            INSERT INTO trader_strategy_versions
                (strategy_id, version, marketmind_spec_id, template, parameters,
                 symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                 backtest_metrics, approved_for_paper, enabled)
            VALUES (%s, 1, %s, 'ma_trend', %s, %s, %s, %s, 10, 10, %s, TRUE, TRUE)
            RETURNING id
            """,
            (
                str(sid),
                str(uuid4()),
                Jsonb({}),
                ["BTC/USDT"],
                ["4h"],
                Decimal("0.005"),
                Jsonb({}),
            ),
        )
        vrow = cur.fetchone()
        assert vrow is not None
        conn.commit()
    return UUID(str(vrow[0]))


def _seed_candle(
    database_url: str,
    *,
    symbol: str = "BTC/USDT",
    open_ts: datetime,
    close: Decimal,
) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_candles
                (symbol, timeframe, open_ts, close_ts, open, high, low, close,
                 volume, is_closed)
            VALUES (%s, '4h', %s, %s, %s, %s, %s, %s, 1000, TRUE)
            """,
            (
                symbol,
                open_ts,
                open_ts + timedelta(hours=4),
                close,
                close,
                close,
                close,
            ),
        )
        conn.commit()


def _seed_buy_fill_and_position(
    database_url: str,
    *,
    version_id: UUID,
    symbol: str = "BTC/USDT",
    entry_ts: datetime,
    entry_price: Decimal,
    size: Decimal,
    stop_price: Decimal = Decimal("0.0001"),
) -> UUID:
    """Seed a complete entry chain: signal + order + fill + OPEN
    position. Mimics what Step 7's executor would have written.
    """
    signal_id = uuid4()
    order_id = uuid4()
    fill_id = uuid4()
    position_id = uuid4()
    fee = entry_price * size * Decimal("0.001")  # 10 bps
    notional = entry_price * size
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators, proposed_entry_price, proposed_stop_price)
            VALUES (%s, %s, %s, '4h', %s, 'BUY', 'seed', %s, %s, %s)
            """,
            (
                str(signal_id),
                str(version_id),
                symbol,
                entry_ts,
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
            (str(order_id), str(signal_id), str(version_id), symbol, size, entry_ts),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size, fee,
                 slippage_bps_applied, notional)
            VALUES (%s, %s, %s, %s, %s, %s, 10, %s)
            """,
            (
                str(fill_id),
                str(order_id),
                entry_ts,
                entry_price,
                size,
                fee,
                notional,
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
                entry_ts,
                size,
                stop_price,
            ),
        )
        conn.commit()
    return position_id


def _close_position_with_sell(
    database_url: str,
    *,
    position_id: UUID,
    version_id: UUID,
    symbol: str,
    exit_ts: datetime,
    exit_price: Decimal,
    size: Decimal,
    realised_pnl: Decimal,
) -> None:
    """Seed the SELL side: signal + FILLED order + fill + position
    flipped to CLOSED.
    """
    signal_id = uuid4()
    order_id = uuid4()
    fill_id = uuid4()
    fee = exit_price * size * Decimal("0.001")
    notional = exit_price * size
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators, proposed_entry_price, proposed_stop_price)
            VALUES (%s, %s, %s, '4h', %s, 'EXIT', 'seed', %s, %s, %s)
            """,
            (
                str(signal_id),
                str(version_id),
                symbol,
                exit_ts,
                Jsonb({}),
                exit_price,
                Decimal("0.0001"),
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side,
                 order_type, requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, %s, 'SELL', 'MARKET', %s, 'FILLED', %s)
            """,
            (str(order_id), str(signal_id), str(version_id), symbol, size, exit_ts),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size, fee,
                 slippage_bps_applied, notional)
            VALUES (%s, %s, %s, %s, %s, %s, 10, %s)
            """,
            (
                str(fill_id),
                str(order_id),
                exit_ts,
                exit_price,
                size,
                fee,
                notional,
            ),
        )
        cur.execute(
            """
            UPDATE trader_paper_positions
            SET status = 'CLOSED', exit_order_id = %s, exit_price = %s, exit_ts = %s,
                realised_pnl = %s, close_reason = 'signal_exit'
            WHERE id = %s
            """,
            (
                str(order_id),
                exit_price,
                exit_ts,
                realised_pnl,
                str(position_id),
            ),
        )
        conn.commit()


# ---- Tests -----------------------------------------------------------------


@pytestmark_integration
def test_cold_start_snapshot_uses_starting_cash(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Empty trader state ⇒ cash = starting, equity = starting,
    peak = starting, drawdown = 0.
    """
    snapshot = compute_and_persist_snapshot(database_url, settings)
    assert snapshot.cash == Decimal("1000")
    assert snapshot.equity == Decimal("1000")
    assert snapshot.peak_equity == Decimal("1000")
    assert snapshot.drawdown == Decimal("0")
    assert snapshot.drawdown_pct == Decimal("0")
    assert snapshot.open_positions_count == 0
    assert snapshot.per_strategy_breakdown == {}
    assert snapshot.per_symbol_breakdown == {}


@pytestmark_integration
def test_buy_fill_reduces_cash_and_mtm_adds_back(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """After one BUY fill at $100, size 1:
    cash = 1000 - (100 + 0.1) = 899.9
    MTM @ latest_close=110 = 110
    equity = 899.9 + 110 = 1009.9 (≈ +0.99% on starting equity)
    unrealised_pnl = (110 - 100) * 1 = 10
    """
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 18, 12, tzinfo=UTC)
    _seed_buy_fill_and_position(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        entry_price=Decimal("100"),
        size=Decimal("1"),
    )
    # Latest closed candle at 110.
    _seed_candle(
        database_url,
        open_ts=entry_ts + timedelta(hours=4),
        close=Decimal("110"),
    )

    snapshot = compute_and_persist_snapshot(database_url, settings)
    assert snapshot.cash == Decimal("899.9")
    assert snapshot.equity == Decimal("1009.9")
    assert snapshot.unrealised_pnl == Decimal("10")
    assert snapshot.open_positions_count == 1
    assert str(version_id) in snapshot.per_strategy_breakdown
    assert "BTC/USDT" in snapshot.per_symbol_breakdown


@pytestmark_integration
def test_full_round_trip_realises_pnl_into_cumulative(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """BUY then SELL: cash reflects both legs; equity == cash (no
    open positions); realised_pnl_cumulative captures the trade's PnL.
    """
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 18, 12, tzinfo=UTC)
    exit_ts = entry_ts + timedelta(hours=4)

    position_id = _seed_buy_fill_and_position(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        entry_price=Decimal("100"),
        size=Decimal("1"),
    )
    # Realised PnL = (110 - 100) * 1 - 0.1 - 0.11 = 9.79
    _close_position_with_sell(
        database_url,
        position_id=position_id,
        version_id=version_id,
        symbol="BTC/USDT",
        exit_ts=exit_ts,
        exit_price=Decimal("110"),
        size=Decimal("1"),
        realised_pnl=Decimal("9.79"),
    )

    snapshot = compute_and_persist_snapshot(database_url, settings)
    # cash = 1000 - 100.1 + (110 - 0.11) = 1009.79
    assert snapshot.cash == Decimal("1009.79")
    assert snapshot.equity == Decimal("1009.79")  # no open positions
    assert snapshot.unrealised_pnl == Decimal("0")
    assert snapshot.realised_pnl_cumulative == Decimal("9.79")
    assert snapshot.open_positions_count == 0


@pytestmark_integration
def test_peak_equity_only_increases(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Successive snapshots: peak tracks max, drawdown reflects gap
    from peak. Peak doesn't decrease even if equity does.
    """
    # Snapshot 1: starting state → equity = 1000.
    s1 = compute_and_persist_snapshot(database_url, settings)
    assert s1.peak_equity == Decimal("1000")

    # Bump equity to 1100 via a closed profitable trade.
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 17, 12, tzinfo=UTC)
    position_id = _seed_buy_fill_and_position(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        entry_price=Decimal("100"),
        size=Decimal("1"),
    )
    _close_position_with_sell(
        database_url,
        position_id=position_id,
        version_id=version_id,
        symbol="BTC/USDT",
        exit_ts=entry_ts + timedelta(hours=4),
        exit_price=Decimal("200"),
        size=Decimal("1"),
        realised_pnl=Decimal("99.7"),  # roughly (200-100)*1 - 0.1 - 0.2
    )
    s2 = compute_and_persist_snapshot(database_url, settings)
    assert s2.equity > s1.equity
    assert s2.peak_equity == s2.equity
    assert s2.drawdown == Decimal("0")

    # Now open a loser to push equity below s2's peak.
    entry_ts_b = datetime(2026, 5, 18, 12, tzinfo=UTC)
    position_id_b = _seed_buy_fill_and_position(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts_b,
        entry_price=Decimal("100"),
        size=Decimal("1"),
    )
    _close_position_with_sell(
        database_url,
        position_id=position_id_b,
        version_id=version_id,
        symbol="BTC/USDT",
        exit_ts=entry_ts_b + timedelta(hours=4),
        exit_price=Decimal("50"),
        size=Decimal("1"),
        realised_pnl=Decimal("-50.15"),  # ~ (50-100)*1 - 0.1 - 0.05
    )
    s3 = compute_and_persist_snapshot(database_url, settings)
    # peak stayed at s2's level despite the loss
    assert s3.peak_equity == s2.peak_equity
    assert s3.equity < s2.equity
    assert s3.drawdown > Decimal("0")
    assert s3.drawdown_pct > Decimal("0")


@pytestmark_integration
def test_fetch_latest_returns_none_when_no_snapshots(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    _ = settings
    assert fetch_latest_snapshot(database_url) is None


@pytestmark_integration
def test_fetch_latest_returns_most_recent(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    compute_and_persist_snapshot(database_url, settings)
    s2 = compute_and_persist_snapshot(database_url, settings)
    latest = fetch_latest_snapshot(database_url)
    assert latest is not None
    assert latest.id == s2.id


@pytestmark_integration
def test_fetch_equity_curve_returns_chronological(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    compute_and_persist_snapshot(database_url, settings)
    compute_and_persist_snapshot(database_url, settings)
    compute_and_persist_snapshot(database_url, settings)
    curve = fetch_equity_curve(database_url)
    from itertools import pairwise

    assert len(curve) == 3
    # Strictly ascending timestamps.
    for prev, curr in pairwise(curve):
        assert curr[0] >= prev[0]


@pytestmark_integration
def test_fetch_equity_curve_respects_since_filter(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    s1 = compute_and_persist_snapshot(database_url, settings)
    s2 = compute_and_persist_snapshot(database_url, settings)
    _ = s1
    # Filter from s2's ts onwards.
    curve = fetch_equity_curve(database_url, since=s2.ts)
    assert len(curve) == 1
    assert curve[0][0] == s2.ts
