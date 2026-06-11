"""Tests for the trader v1 paper execution engine.

Layer 1: PURE unit tests for `_compute_fill` (no DB, no network).
Layer 2: integration tests for `process_one_cycle` via
testcontainers + the trader migrations. All integration tests
carry `@pytest.mark.integration`.

The single MOST IMPORTANT integration test is
`test_fill_matches_candle_by_open_ts_not_close_ts` — it proves
that off-by-one bugs in the fill-candle lookup are caught.
Lookahead bias here would silently inflate paper returns vs
backtest; fill-too-late would deflate them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from marketmind_shared.schemas.trader import OrderSide
from marketmind_workers.trader.config import TraderSettings, get_trader_settings
from marketmind_workers.trader.execution import (
    _compute_fill,
    process_one_cycle,
)
from psycopg.types.json import Jsonb

# ---- Layer 1: pure unit tests for _compute_fill ----------------------------


class TestComputeFill:
    """Parametric pure-function tests. Each asserts the canonical
    `fill_price = ref * (1 ± slippage_bps/10000)` and
    `fee = fill_price * size * fee_bps/10000` formulas, plus
    `notional = fill_price * size`.
    """

    def test_buy_adds_slippage_above_reference(self) -> None:
        result = _compute_fill(
            side=OrderSide.BUY,
            reference_price=Decimal("100"),
            size=Decimal("1"),
            slippage_bps=Decimal("10"),  # 0.1%
            fee_bps=Decimal("10"),  # 0.1%
        )
        # 100 * (1 + 0.001) = 100.10
        assert result.fill_price == Decimal("100.10000000")
        # fee = 100.10 * 1 * 0.001 = 0.10010 → quantised at 8dp
        assert result.fee == Decimal("0.10010000")
        assert result.notional == Decimal("100.10000000")
        assert result.slippage_bps_applied == Decimal("10")

    def test_sell_subtracts_slippage_below_reference(self) -> None:
        result = _compute_fill(
            side=OrderSide.SELL,
            reference_price=Decimal("100"),
            size=Decimal("1"),
            slippage_bps=Decimal("10"),
            fee_bps=Decimal("10"),
        )
        # 100 * (1 - 0.001) = 99.90
        assert result.fill_price == Decimal("99.90000000")
        assert result.fee == Decimal("0.09990000")

    def test_zero_slippage_means_fill_at_reference(self) -> None:
        for side in (OrderSide.BUY, OrderSide.SELL):
            result = _compute_fill(
                side=side,
                reference_price=Decimal("100"),
                size=Decimal("1"),
                slippage_bps=Decimal("0"),
                fee_bps=Decimal("10"),
            )
            assert result.fill_price == Decimal("100.00000000")

    def test_zero_fee_means_zero_fee_field(self) -> None:
        result = _compute_fill(
            side=OrderSide.BUY,
            reference_price=Decimal("100"),
            size=Decimal("1"),
            slippage_bps=Decimal("10"),
            fee_bps=Decimal("0"),
        )
        assert result.fee == Decimal("0E-8")

    def test_size_scales_fee_and_notional_linearly(self) -> None:
        result_one = _compute_fill(
            side=OrderSide.BUY,
            reference_price=Decimal("100"),
            size=Decimal("1"),
            slippage_bps=Decimal("10"),
            fee_bps=Decimal("10"),
        )
        result_ten = _compute_fill(
            side=OrderSide.BUY,
            reference_price=Decimal("100"),
            size=Decimal("10"),
            slippage_bps=Decimal("10"),
            fee_bps=Decimal("10"),
        )
        # 10x size ⇒ 10x fee + 10x notional. fill_price unchanged.
        assert result_ten.fee == result_one.fee * 10
        assert result_ten.notional == result_one.notional * 10
        assert result_ten.fill_price == result_one.fill_price


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
    """Reset trader tables between tests. CASCADE wipes the chain."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE trader_strategies, trader_candles, "
            "trader_portfolio_snapshots, trader_alerts, trader_risk_events, "
            "trader_audit_logs RESTART IDENTITY CASCADE",
        )
        conn.commit()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TRADER_SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("TRADER_TIMEFRAMES", "4h")
    get_trader_settings.cache_clear()
    return get_trader_settings()


# ---- Seed helpers ----------------------------------------------------------


def _seed_version(
    database_url: str,
    *,
    fee_bps: Decimal = Decimal("10"),
    slippage_bps: Decimal = Decimal("10"),
) -> UUID:
    name = f"exec-test-{uuid4().hex[:8]}"
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
            VALUES (%s, 1, %s, 'ma_trend', %s, %s, %s, %s, %s, %s, %s, TRUE, TRUE)
            RETURNING id
            """,
            (
                str(sid),
                str(uuid4()),
                Jsonb({}),
                ["BTC/USDT"],
                ["4h"],
                Decimal("0.005"),
                fee_bps,
                slippage_bps,
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
    open_ts: datetime,
    open_price: Decimal = Decimal("100"),
    high: Decimal | None = None,
    low: Decimal | None = None,
    close: Decimal | None = None,
) -> None:
    h = high if high is not None else open_price * Decimal("1.001")
    lo = low if low is not None else open_price * Decimal("0.999")
    c = close if close is not None else open_price
    close_ts = open_ts + timedelta(hours=4)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_candles
                (symbol, timeframe, open_ts, close_ts, open, high, low, close,
                 volume, is_closed)
            VALUES ('BTC/USDT', '4h', %s, %s, %s, %s, %s, %s, 1000, TRUE)
            """,
            (open_ts, close_ts, open_price, h, lo, c),
        )
        conn.commit()


def _seed_pending_buy_order(
    database_url: str,
    *,
    version_id: UUID,
    intended_fill_ts: datetime,
    requested_size: Decimal = Decimal("1"),
    proposed_stop_price: Decimal = Decimal("95"),
    proposed_take_profit_price: Decimal | None = None,
) -> tuple[UUID, UUID]:
    """Seed a BUY signal + pending order. Returns (signal_id, order_id)."""
    signal_id = uuid4()
    order_id = uuid4()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators,
                 proposed_entry_price, proposed_stop_price, proposed_take_profit_price)
            VALUES (%s, %s, 'BTC/USDT', '4h', %s, 'BUY', 'test', %s,
                    %s, %s, %s)
            """,
            (
                str(signal_id),
                str(version_id),
                intended_fill_ts - timedelta(hours=4),
                Jsonb({}),
                Decimal("100"),
                proposed_stop_price,
                proposed_take_profit_price,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side,
                 order_type, requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, 'BTC/USDT', 'BUY', 'MARKET', %s, 'PENDING', %s)
            """,
            (
                str(order_id),
                str(signal_id),
                str(version_id),
                requested_size,
                intended_fill_ts,
            ),
        )
        conn.commit()
    return signal_id, order_id


def _seed_pending_exit_order(
    database_url: str,
    *,
    version_id: UUID,
    intended_fill_ts: datetime,
    size: Decimal,
) -> tuple[UUID, UUID]:
    """Seed an EXIT signal + pending order. Returns (signal_id, order_id)."""
    signal_id = uuid4()
    order_id = uuid4()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators,
                 proposed_entry_price, proposed_stop_price)
            VALUES (%s, %s, 'BTC/USDT', '4h', %s, 'EXIT', 'test exit', %s,
                    %s, %s)
            """,
            (
                str(signal_id),
                str(version_id),
                intended_fill_ts - timedelta(hours=4),
                Jsonb({}),
                Decimal("100"),
                Decimal("90"),  # placeholder
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side,
                 order_type, requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, 'BTC/USDT', 'SELL', 'MARKET', %s, 'PENDING', %s)
            """,
            (
                str(order_id),
                str(signal_id),
                str(version_id),
                size,
                intended_fill_ts,
            ),
        )
        conn.commit()
    return signal_id, order_id


def _seed_open_position_with_entry(
    database_url: str,
    *,
    version_id: UUID,
    entry_ts: datetime,
    entry_price: Decimal = Decimal("100"),
    size: Decimal = Decimal("1"),
    stop_price: Decimal = Decimal("90"),
    take_profit_price: Decimal | None = None,
) -> tuple[UUID, UUID]:
    """Seed an OPEN position with its entry fill row (so close-side
    realised_pnl math can look up entry_fee correctly). Returns
    (position_id, entry_order_id).
    """
    signal_id, order_id = _seed_pending_buy_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=entry_ts,
        requested_size=size,
        proposed_stop_price=stop_price,
    )
    position_id = uuid4()
    fill_id = uuid4()
    # Compute entry fee using the standard formula so realised_pnl
    # math during close matches reality.
    fee_bps = Decimal("10")
    notional = entry_price * size
    fee = notional * fee_bps / Decimal("10000")
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trader_paper_orders SET status = 'FILLED' WHERE id = %s",
            (str(order_id),),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size, fee,
                 slippage_bps_applied, notional)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(fill_id),
                str(order_id),
                entry_ts,
                entry_price,
                size,
                fee,
                Decimal("10"),
                notional,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_positions
                (id, strategy_version_id, symbol, side, entry_order_id,
                 entry_price, entry_ts, size, stop_price, take_profit_price,
                 status)
            VALUES (%s, %s, 'BTC/USDT', 'LONG', %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (
                str(position_id),
                str(version_id),
                str(order_id),
                entry_price,
                entry_ts,
                size,
                stop_price,
                take_profit_price,
            ),
        )
        conn.commit()
    _ = signal_id  # avoid linter complaint
    return position_id, order_id


# ---- The load-bearing test: fill candle matched by open_ts ----------------


@pytestmark_integration
def test_fill_matches_candle_by_open_ts_not_close_ts(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Off-by-one bug guard.

    Setup: order with `intended_fill_ts = T`. Two candles in the DB:
      - Candle A: open_ts = T, open = 100. The correct fill candle.
      - Candle B: open_ts = T - 4h, close_ts = T, open = 999.
        If the executor wrongly matches on close_ts, it would
        fill at 999's open (with slippage) instead of 100's.

    Expected fill_price ≈ 100 * (1 + 0.001) = 100.10. NOT 999.999.
    """
    version_id = _seed_version(database_url)
    intended_fill_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)

    # Candle A — the correct fill candle.
    _seed_candle(
        database_url,
        open_ts=intended_fill_ts,
        open_price=Decimal("100"),
    )
    # Candle B — the DECOY. Its close_ts == intended_fill_ts but
    # its open is 999 (a wildly different number so a bug is obvious).
    _seed_candle(
        database_url,
        open_ts=intended_fill_ts - timedelta(hours=4),
        open_price=Decimal("999"),
    )

    _, order_id = _seed_pending_buy_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=intended_fill_ts,
    )

    result = process_one_cycle(database_url, settings)

    assert result.pending_orders_filled == 1
    assert result.positions_opened == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT fill_price FROM trader_paper_fills WHERE order_id = %s",
            (str(order_id),),
        )
        row = cur.fetchone()
        assert row is not None
        # Fill at candle A's open (100) with +0.1% slippage = 100.10.
        # If the executor had wrongly matched candle B (open=999),
        # we'd see ~999.999 here.
        assert row[0] == Decimal("100.10000000"), (
            f"executor filled at {row[0]} — likely matched candle by "
            f"close_ts instead of open_ts (decoy candle's open was 999)"
        )


# ---- BUY fill basics -------------------------------------------------------


@pytestmark_integration
def test_buy_fill_opens_position_at_next_bar_open(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """End-to-end happy path: BUY pending order → fill row +
    OPEN trader_paper_positions row with correct entry_price.
    """
    version_id = _seed_version(database_url)
    intended_fill_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    _seed_candle(database_url, open_ts=intended_fill_ts, open_price=Decimal("100"))
    _, order_id = _seed_pending_buy_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=intended_fill_ts,
        requested_size=Decimal("0.5"),
    )

    result = process_one_cycle(database_url, settings)
    assert result.pending_orders_filled == 1
    assert result.positions_opened == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM trader_paper_orders WHERE id = %s",
            (str(order_id),),
        )
        order_row = cur.fetchone()
        assert order_row is not None
        assert order_row[0] == "FILLED"

        cur.execute(
            """
            SELECT entry_price, entry_ts, size, status
            FROM trader_paper_positions
            WHERE strategy_version_id = %s AND symbol = 'BTC/USDT'
            """,
            (str(version_id),),
        )
        pos = cur.fetchone()
        assert pos is not None
        entry_price, entry_ts, size, status = pos
        assert entry_price == Decimal("100.10000000")
        assert entry_ts == intended_fill_ts
        assert size == Decimal("0.5")
        assert status == "OPEN"


@pytestmark_integration
def test_pending_order_waits_when_candle_not_yet_ingested(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """An order whose `intended_fill_ts` has no candle in
    trader_candles is left PENDING for the next cycle.
    """
    version_id = _seed_version(database_url)
    intended_fill_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    # Note: no candle seeded.
    _, order_id = _seed_pending_buy_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=intended_fill_ts,
    )

    result = process_one_cycle(database_url, settings)

    assert result.pending_orders_waiting == 1
    assert result.pending_orders_filled == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM trader_paper_orders WHERE id = %s",
            (str(order_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "PENDING"


# ---- EXIT fill (signal-driven) --------------------------------------------


@pytestmark_integration
def test_exit_fill_closes_position_with_correct_realised_pnl(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """EXIT order on an open position:
    - fill_price = candle_open * (1 - slippage_bps/10000)
    - realised_pnl = (exit_price - entry_price) * size - entry_fee - exit_fee
    - close_reason = 'signal_exit'
    """
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    exit_ts = entry_ts + timedelta(hours=4)

    position_id, _entry_order_id = _seed_open_position_with_entry(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_price=Decimal("90"),
    )
    # Entry candle (for stop check; no breach here).
    _seed_candle(
        database_url,
        open_ts=entry_ts,
        open_price=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
    )
    # Exit candle: open=110 → fill at 110 * 0.999 = 109.89.
    _seed_candle(database_url, open_ts=exit_ts, open_price=Decimal("110"))
    _, _exit_order_id = _seed_pending_exit_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=exit_ts,
        size=Decimal("1"),
    )

    result = process_one_cycle(database_url, settings)
    assert result.positions_closed_by_signal == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, exit_price, exit_ts, realised_pnl, close_reason "
            "FROM trader_paper_positions WHERE id = %s",
            (str(position_id),),
        )
        row = cur.fetchone()
        assert row is not None
        status, exit_price, exit_ts_actual, realised_pnl, close_reason = row
        assert status == "CLOSED"
        assert exit_price == Decimal("109.89000000")
        assert exit_ts_actual == exit_ts
        assert close_reason == "signal_exit"
        # entry_price=100, entry_fee=100*1*0.001=0.1
        # exit_price=109.89, exit_fee=109.89*1*0.001=0.10989
        # realised_pnl = (109.89-100)*1 - 0.1 - 0.10989 = 9.68011
        assert realised_pnl == pytest.approx(Decimal("9.68011"), abs=Decimal("0.001"))


# ---- Stop-hit force-close --------------------------------------------------


@pytestmark_integration
def test_stop_hit_force_closes_position(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """A candle with `low <= stop_price` force-closes the position
    via the synthetic signal+order+fill chain.
    """
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    breach_ts = entry_ts + timedelta(hours=4)

    position_id, _ = _seed_open_position_with_entry(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_price=Decimal("95"),
    )
    # Entry bar: well above the stop.
    _seed_candle(
        database_url,
        open_ts=entry_ts,
        open_price=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
    )
    # Breach bar: low = 94 ≤ stop 95 → stop hit.
    _seed_candle(
        database_url,
        open_ts=breach_ts,
        open_price=Decimal("96"),
        high=Decimal("96"),
        low=Decimal("94"),
        close=Decimal("94"),
    )

    result = process_one_cycle(database_url, settings)
    assert result.positions_closed_by_stop == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, exit_price, exit_ts, close_reason "
            "FROM trader_paper_positions WHERE id = %s",
            (str(position_id),),
        )
        row = cur.fetchone()
        assert row is not None
        status, exit_price, exit_ts, close_reason = row
        assert status == "CLOSED"
        # stop_price=95, slippage=10 bps ⇒ fill = 95 * 0.999 = 94.905.
        assert exit_price == Decimal("94.90500000")
        assert exit_ts == breach_ts
        assert close_reason == "stop_hit"

        # Synthetic signal + order + fill rows exist.
        cur.execute(
            "SELECT COUNT(*) FROM trader_signals WHERE strategy_version_id = %s "
            "AND reason = 'stop_hit'",
            (str(version_id),),
        )
        scnt = cur.fetchone()
        assert scnt is not None
        assert scnt[0] == 1


@pytestmark_integration
def test_stop_check_runs_before_pending_exit(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Race condition: a PENDING EXIT order exists, AND the position's
    stop was breached. The stop check runs first; the EXIT order
    is rejected with `position_already_closed` because the
    position is no longer OPEN by the time Phase 2 runs.
    """
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    breach_ts = entry_ts + timedelta(hours=4)

    position_id, _ = _seed_open_position_with_entry(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        stop_price=Decimal("95"),
    )
    _seed_candle(
        database_url,
        open_ts=entry_ts,
        open_price=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
    )
    # Breach bar AND the candle the EXIT order wants to fill at.
    _seed_candle(
        database_url,
        open_ts=breach_ts,
        open_price=Decimal("96"),
        high=Decimal("96"),
        low=Decimal("94"),
        close=Decimal("94"),
    )
    _, exit_order_id = _seed_pending_exit_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=breach_ts,
        size=Decimal("1"),
    )

    result = process_one_cycle(database_url, settings)

    assert result.positions_closed_by_stop == 1
    assert result.pending_orders_rejected == 1
    assert result.positions_closed_by_signal == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, close_reason FROM trader_paper_positions WHERE id = %s",
            (str(position_id),),
        )
        prow = cur.fetchone()
        assert prow is not None
        # Stop closed it, not the signal.
        assert prow[0] == "CLOSED"
        assert prow[1] == "stop_hit"

        cur.execute(
            "SELECT status, rejection_reason FROM trader_paper_orders WHERE id = %s",
            (str(exit_order_id),),
        )
        orow = cur.fetchone()
        assert orow is not None
        assert orow[0] == "REJECTED"
        assert orow[1] == "position_already_closed"


@pytestmark_integration
def test_same_bar_stop_hit_on_entry_candle(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """If the ENTRY bar's low <= stop_price, the position is
    closed in the same cycle it would have been scanned. Matches
    vbt's same-bar stop semantic.
    """
    version_id = _seed_version(database_url)
    entry_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)

    position_id, _ = _seed_open_position_with_entry(
        database_url,
        version_id=version_id,
        entry_ts=entry_ts,
        entry_price=Decimal("100"),
        size=Decimal("1"),
        stop_price=Decimal("99"),
    )
    # Entry bar has low=98 ≤ stop=99 — same-bar stop.
    _seed_candle(
        database_url,
        open_ts=entry_ts,
        open_price=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("98"),
        close=Decimal("99"),
    )

    result = process_one_cycle(database_url, settings)
    assert result.positions_closed_by_stop == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, close_reason FROM trader_paper_positions WHERE id = %s",
            (str(position_id),),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "CLOSED"
        assert row[1] == "stop_hit"


# ---- Idempotency -----------------------------------------------------------


@pytestmark_integration
def test_repeated_cycles_do_not_double_fill(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Running the cycle twice fills the order once. The second
    cycle finds no PENDING orders (filled), no OPEN positions
    (still open with no breach), and no work to do.
    """
    version_id = _seed_version(database_url)
    intended_fill_ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    _seed_candle(database_url, open_ts=intended_fill_ts, open_price=Decimal("100"))
    _seed_pending_buy_order(
        database_url,
        version_id=version_id,
        intended_fill_ts=intended_fill_ts,
    )

    first = process_one_cycle(database_url, settings)
    second = process_one_cycle(database_url, settings)

    assert first.pending_orders_filled == 1
    assert first.positions_opened == 1
    assert second.pending_orders_filled == 0
    assert second.positions_opened == 0
    # Phase 1 still scans the now-OPEN position — no breach → 0 closes.
    assert second.open_positions_scanned == 1
    assert second.positions_closed_by_stop == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_paper_fills")
        rc = cur.fetchone()
        assert rc is not None
        assert rc[0] == 1
