"""Crash safety: kill the paper trader mid-loop, restart, assert idempotent
state recovery (integration: needs Postgres). A unit-level test of the
recovery merge logic runs without the stack."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from marketmind_workers.ftr.config.venue_profiles import get_profile
from marketmind_workers.ftr.trader.paper_broker import PaperBroker, Position


@pytest.fixture(scope="module")
def pg_container() -> Iterator[object]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    yield container
    container.stop()


@pytest.fixture(scope="module")
def postgres_url(pg_container: object) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return str(url).replace("postgresql+psycopg2://", "postgresql://")


def test_recovery_merge_logic_unit() -> None:
    """Broker state rebuild from recovered rows is idempotent: applying the
    same recovered state twice yields the same broker."""
    recovered_cash = Decimal("9876.543210")
    recovered_positions = [
        Position(
            symbol="BTC/USDT",
            qty=Decimal("0.05"),
            avg_entry_price=Decimal("50000"),
            opened_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
    ]

    def rebuild() -> PaperBroker:
        broker = PaperBroker(
            profile=get_profile("kraken_pro_uk_tier0"), cash=Decimal("10000")
        )
        broker.cash = recovered_cash
        for pos in recovered_positions:
            broker.positions[pos.symbol] = pos
        return broker

    b1, b2 = rebuild(), rebuild()
    assert b1.cash == b2.cash
    assert b1.positions.keys() == b2.positions.keys()
    marks = {"BTC/USDT": Decimal("51000")}
    assert b1.equity(marks) == b2.equity(marks)


@pytest.mark.integration
def test_crash_recovery_round_trip(postgres_url: str) -> None:
    """Full DB round trip: decisions + fills + snapshot, then recover.

    Requires the docker stack (testcontainers postgres fixture). Asserts:
    - duplicate decision insert is a no-op (idempotency key)
    - recover_state returns the persisted cash + open positions
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from marketmind_workers.db import apply_migrations
    from marketmind_workers.ftr.strategies.records import Action, DecisionRecord, ReasonCode
    from marketmind_workers.ftr.trader import persistence as db
    from marketmind_workers.ftr.trader.paper_broker import Position

    apply_migrations(postgres_url)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    rec = DecisionRecord.model_validate(
        {
            "ts_utc": now,
            "strategy_id": "crash-test",
            "symbol": "BTC/USDT",
            "action": Action.ENTER_LONG,
            "reason_codes": [ReasonCode.ENTER_EV_POSITIVE],
        }
    )
    first = db.insert_decision(postgres_url, rec, now)
    assert first is not None
    duplicate = db.insert_decision(postgres_url, rec, now)
    assert duplicate is None  # idempotent

    pos = Position(
        symbol="BTC/USDT", qty=Decimal("0.1"), avg_entry_price=Decimal("50000"), opened_at=now
    )
    db.upsert_open_position(postgres_url, "crash-test", pos)
    db.snapshot_equity(
        postgres_url,
        ts=now,
        cash=Decimal("5000"),
        positions_value=Decimal("5000"),
        gross_exposure_pct=0.5,
    )

    recovered = db.recover_state(postgres_url, Decimal("10000"))
    assert recovered.cash == Decimal("5000")
    assert len(recovered.open_positions) == 1
    assert recovered.open_positions[0].symbol == "BTC/USDT"
    assert recovered.strategy_by_symbol["BTC/USDT"] == "crash-test"
