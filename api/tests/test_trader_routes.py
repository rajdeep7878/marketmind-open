"""Tests for the trader read routes under /trader/*.

Mirrors the test_admin.py pattern: stubs the DB helpers in
`marketmind_api.trader.read` via monkeypatch so the tests stay
hermetic — no testcontainers, no real Postgres. The DB read
queries themselves are exercised by the worker integration tests
(opt-in `@pytest.mark.integration`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from marketmind_api.trader import read as trader_read
from marketmind_shared.schemas.strategy_spec.common import Timeframe
from marketmind_shared.schemas.trader import (
    Alert,
    AlertChannel,
    AuditLog,
    DriftMetric,
    HealthStatus,
    LoopName,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperFill,
    PaperOrder,
    PaperPosition,
    PortfolioSnapshot,
    PositionSide,
    PositionStatus,
    RunStatus,
    Severity,
    Signal,
    SignalKind,
)

# A small fixed UTC timestamp the tests reuse for all "ts" fields so
# the responses are stable + comparisons in tests don't drift.
_T = datetime(2026, 5, 18, 12, 0, 0, tzinfo=UTC)


# ---- Sample-row builders ---------------------------------------------------


def _sample_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        id=1,
        ts=_T,
        cash=Decimal("9500.00"),
        equity=Decimal("10250.00"),
        unrealised_pnl=Decimal("250.00"),
        realised_pnl_cumulative=Decimal("500.00"),
        peak_equity=Decimal("10500.00"),
        drawdown=Decimal("250.00"),
        drawdown_pct=Decimal("0.0238"),
        open_positions_count=1,
        per_strategy_breakdown={},
        per_symbol_breakdown={},
    )


def _sample_position(status: PositionStatus = PositionStatus.OPEN) -> PaperPosition:
    return PaperPosition(
        id=uuid4(),
        strategy_version_id=uuid4(),
        symbol="BTC/USDT",
        side=PositionSide.LONG,
        entry_order_id=uuid4(),
        exit_order_id=None,
        entry_price=Decimal("60000"),
        entry_ts=_T,
        exit_price=None,
        exit_ts=None,
        size=Decimal("0.01"),
        stop_price=Decimal("59000"),
        take_profit_price=None,
        status=status,
        realised_pnl=None,
        realised_pnl_pct=None,
        close_reason=None,
    )


def _sample_signal() -> Signal:
    return Signal(
        id=uuid4(),
        strategy_version_id=uuid4(),
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        candle_close_ts=_T,
        signal=SignalKind.BUY,
        reason="ma_cross",
        indicators={"fast_ma": 60100.0, "slow_ma": 59900.0},
        proposed_entry_price=Decimal("60000"),
        proposed_stop_price=Decimal("59000"),
        proposed_take_profit_price=None,
        created_at=_T,
        processed_at=None,
    )


def _sample_order() -> PaperOrder:
    return PaperOrder(
        id=uuid4(),
        signal_id=uuid4(),
        strategy_version_id=uuid4(),
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        requested_size=Decimal("0.01"),
        requested_at=_T,
        status=OrderStatus.PENDING,
        rejection_reason=None,
        intended_fill_ts=_T,
    )


def _sample_fill() -> PaperFill:
    return PaperFill(
        id=uuid4(),
        order_id=uuid4(),
        fill_ts=_T,
        fill_price=Decimal("60050"),
        size=Decimal("0.01"),
        fee=Decimal("0.60"),
        slippage_bps_applied=Decimal("8.33"),
        notional=Decimal("600.50"),
    )


def _sample_alert() -> Alert:
    return Alert(
        id=uuid4(),
        ts=_T,
        channel=AlertChannel.TELEGRAM,
        severity=Severity.WARNING,
        subject="data_feed_failure",
        body="3 consecutive failures fetching BTC/USDT",
        delivered=True,
        delivery_error=None,
    )


def _sample_audit() -> AuditLog:
    return AuditLog(
        id=42,
        ts=_T,
        actor="bot",
        event="position_opened",
        entity_type="paper_position",
        entity_id=str(uuid4()),
        payload={"symbol": "BTC/USDT"},
    )


def _sample_drift() -> DriftMetric:
    return DriftMetric(
        id=uuid4(),
        ts=_T,
        strategy_version_id=uuid4(),
        window_label="trailing_30d",
        paper_trade_count=12,
        paper_win_rate=Decimal("0.55"),
        paper_avg_return_per_trade=Decimal("0.012"),
        paper_current_drawdown_pct=Decimal("0.04"),
        backtest_trade_freq_per_week=Decimal("3.0"),
        backtest_win_rate=Decimal("0.60"),
        backtest_avg_return_per_trade=Decimal("0.015"),
        backtest_max_drawdown_pct=Decimal("0.08"),
        trade_freq_ratio=Decimal("1.10"),
        win_rate_delta=Decimal("-0.05"),
        avg_return_delta=Decimal("-0.003"),
        drawdown_ratio=Decimal("0.50"),
        health_status=HealthStatus.HEALTHY,
    )


# ---- Typed stub helpers ----------------------------------------------------
#
# pyright (strict) rejects bare `lambda _url: value` because the
# parameter types are unknown. These helpers wrap the same logic
# behind typed inner functions so each test stays a one-liner.


def _stub_url(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: Any,
) -> None:
    """Stub a `(_url: str) -> Any` read helper to return `value`."""

    def _fake(_url: str) -> Any:
        return value

    monkeypatch.setattr(trader_read, name, _fake)


def _stub_url_limit_offset(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: Any,
) -> None:
    """Stub a `(_url: str, *, limit: int, offset: int) -> Any` helper."""

    def _fake(_url: str, *, limit: int, offset: int) -> Any:
        _ = limit, offset
        return value

    monkeypatch.setattr(trader_read, name, _fake)


def _stub_url_limit(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: Any,
) -> None:
    """Stub a `(_url: str, *, limit: int) -> Any` helper."""

    def _fake(_url: str, *, limit: int) -> Any:
        _ = limit
        return value

    monkeypatch.setattr(trader_read, name, _fake)


# ---- /trader/health --------------------------------------------------------


def _sample_bot_run(
    *,
    status_val: RunStatus = RunStatus.RUNNING,
    last_heartbeat_at: datetime = _T,
) -> tuple[Any, LoopName, RunStatus, datetime, datetime, str]:
    return (
        uuid4(),
        LoopName.RUNNER,
        status_val,
        _T,
        last_heartbeat_at,
        "phase=signal",
    )


def test_health_returns_run_snapshot_and_now(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: both signals populated + server now is a tz-aware
    UTC datetime.
    """
    run = _sample_bot_run()
    snap = _sample_snapshot()
    _stub_url(monkeypatch, "fetch_latest_bot_run", run)
    _stub_url(monkeypatch, "fetch_latest_snapshot", snap)

    resp = client.get("/trader/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest_run"] is not None
    assert body["latest_run"]["status"] == "running"
    assert body["latest_run"]["loop_name"] == "runner"
    assert body["latest_run"]["last_heartbeat_at"] == _T.isoformat().replace(
        "+00:00", "Z",
    ) or body["latest_run"]["last_heartbeat_at"].startswith(_T.isoformat()[:19])
    assert body["last_snapshot_ts"] is not None
    # `now` is a tz-aware ISO string ending in Z or +00:00.
    assert "T" in body["now"]


def test_health_returns_nulls_when_nothing_recorded(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-first-boot state: no bot run, no snapshot. The dashboard
    treats this as "bot has never been run" — distinct from "bot
    died".
    """
    _stub_url(monkeypatch, "fetch_latest_bot_run", None)
    _stub_url(monkeypatch, "fetch_latest_snapshot", None)
    resp = client.get("/trader/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["latest_run"] is None
    assert body["last_snapshot_ts"] is None


def test_health_surfaces_crashed_status(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the stale-detector has marked a run 'crashed', the
    health endpoint exposes that — the dashboard can render a red
    indicator without polling a separate endpoint.
    """
    run = _sample_bot_run(status_val=RunStatus.CRASHED)
    _stub_url(monkeypatch, "fetch_latest_bot_run", run)
    _stub_url(monkeypatch, "fetch_latest_snapshot", None)
    resp = client.get("/trader/health")
    assert resp.status_code == 200, resp.text
    assert resp.json()["latest_run"]["status"] == "crashed"


# ---- /trader/portfolio/* ---------------------------------------------------


def test_portfolio_current_returns_latest_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _sample_snapshot()
    _stub_url(monkeypatch, "fetch_latest_snapshot", snap)
    resp = client.get("/trader/portfolio/current")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert Decimal(body["equity"]) == snap.equity
    assert body["open_positions_count"] == 1


def test_portfolio_current_returns_null_when_no_snapshot(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_url(monkeypatch, "fetch_latest_snapshot", None)
    resp = client.get("/trader/portfolio/current")
    assert resp.status_code == 200, resp.text
    assert resp.json() is None


def test_portfolio_equity_curve_returns_ascending_points(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    points = [
        (datetime(2026, 5, 17, 12, 0, tzinfo=UTC), Decimal("10000")),
        (datetime(2026, 5, 18, 12, 0, tzinfo=UTC), Decimal("10250")),
    ]

    def _fake(
        _url: str,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> list[tuple[datetime, Decimal]]:
        _ = since, until
        return points

    monkeypatch.setattr(trader_read, "fetch_equity_curve", _fake)
    resp = client.get("/trader/portfolio/equity_curve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["points"]) == 2
    assert Decimal(body["points"][0]["equity"]) == Decimal("10000")
    assert Decimal(body["points"][1]["equity"]) == Decimal("10250")


def test_portfolio_equity_curve_passes_since_until(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, datetime | None] = {}

    def _fake(
        _url: str,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> list[tuple[datetime, Decimal]]:
        captured["since"] = since
        captured["until"] = until
        return []

    monkeypatch.setattr(trader_read, "fetch_equity_curve", _fake)
    iso = "2026-05-17T00:00:00Z"
    resp = client.get(f"/trader/portfolio/equity_curve?since={iso}&until={iso}")
    assert resp.status_code == 200, resp.text
    assert captured["since"] is not None
    assert captured["until"] is not None


# ---- /trader/positions/* ---------------------------------------------------


def test_positions_open_returns_list(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_url(monkeypatch, "fetch_open_positions", [_sample_position()])
    resp = client.get("/trader/positions/open")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == "OPEN"


def test_positions_closed_supports_pagination(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, int] = {}

    def _fake(_url: str, *, limit: int, offset: int) -> list[PaperPosition]:
        captured["limit"] = limit
        captured["offset"] = offset
        return [_sample_position(status=PositionStatus.CLOSED)]

    monkeypatch.setattr(trader_read, "fetch_closed_positions", _fake)
    resp = client.get("/trader/positions/closed?limit=25&offset=10")
    assert resp.status_code == 200, resp.text
    assert captured == {"limit": 25, "offset": 10}
    body = resp.json()
    assert body["limit"] == 25
    assert body["offset"] == 10


def test_positions_closed_rejects_invalid_pagination(client: TestClient) -> None:
    resp = client.get("/trader/positions/closed?limit=0")
    assert resp.status_code == 422
    resp = client.get("/trader/positions/closed?limit=501")
    assert resp.status_code == 422
    resp = client.get("/trader/positions/closed?offset=-1")
    assert resp.status_code == 422


# ---- /trader/signals|orders|fills/recent ----------------------------------


def test_signals_recent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_url_limit_offset(monkeypatch, "fetch_recent_signals", [_sample_signal()])
    resp = client.get("/trader/signals/recent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["signal"] == "BUY"


def test_orders_recent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_url_limit_offset(monkeypatch, "fetch_recent_orders", [_sample_order()])
    resp = client.get("/trader/orders/recent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["side"] == "BUY"


def test_fills_recent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_url_limit_offset(monkeypatch, "fetch_recent_fills", [_sample_fill()])
    resp = client.get("/trader/fills/recent")
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["items"][0]["fill_price"]) == Decimal("60050")


# ---- /trader/strategies, drift, alerts, audit ------------------------------


def test_strategies_combines_version_and_latest_drift(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "id": str(uuid4()),
        "strategy_id": str(uuid4()),
        "version": 1,
        "template": "ma_trend",
        "symbols": ["BTC/USDT"],
        "timeframes": ["1h"],
        "risk_pct": "0.005",
        "enabled": True,
        "approved_for_paper": True,
        "created_at": _T,
        "latest_drift_health": HealthStatus.HEALTHY.value,
        "latest_drift_ts": _T,
        "latest_drift_window": "trailing_30d",
    }
    _stub_url(monkeypatch, "fetch_strategy_versions_with_latest_drift", [row])
    resp = client.get("/trader/strategies")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"][0]["latest_drift_health"] == HealthStatus.HEALTHY.value
    assert body["items"][0]["template"] == "ma_trend"


def test_drift_latest(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_url(monkeypatch, "fetch_latest_drift_per_version", [_sample_drift()])
    resp = client.get("/trader/drift/latest")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["health_status"] == HealthStatus.HEALTHY.value


def test_alerts_recent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_url_limit_offset(monkeypatch, "fetch_recent_alerts", [_sample_alert()])
    resp = client.get("/trader/alerts/recent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["channel"] == "telegram"


def test_audit_recent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_url_limit_offset(monkeypatch, "fetch_recent_audit", [_sample_audit()])
    resp = client.get("/trader/audit/recent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["event"] == "position_opened"


# ---- /trader/risk/status --------------------------------------------------


def test_risk_status_no_snapshot_no_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_url(monkeypatch, "fetch_latest_snapshot", None)
    _stub_url_limit(monkeypatch, "fetch_recent_risk_events", [])
    resp = client.get("/trader/risk/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kill_switch_tripped"] is False
    assert body["cash"] is None
    assert body["equity"] is None
    assert body["recent_risk_events"] == []


def test_risk_status_kill_switch_event_flips_flag(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _sample_snapshot()
    _stub_url(monkeypatch, "fetch_latest_snapshot", snap)
    _stub_url_limit(
        monkeypatch,
        "fetch_recent_risk_events",
        [
            {
                "id": str(uuid4()),
                "ts": _T,
                "event_type": "kill_switch",
                "severity": "critical",
                "strategy_version_id": None,
                "symbol": None,
                "details": {"drawdown_pct": "0.21"},
            },
        ],
    )
    resp = client.get("/trader/risk/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kill_switch_tripped"] is True
    assert Decimal(body["equity"]) == snap.equity
    assert body["recent_risk_events"][0]["event_type"] == "kill_switch"


def test_risk_status_non_kill_switch_events_do_not_trip_flag(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snap = _sample_snapshot()
    _stub_url(monkeypatch, "fetch_latest_snapshot", snap)
    _stub_url_limit(
        monkeypatch,
        "fetch_recent_risk_events",
        [
            {
                "id": str(uuid4()),
                "ts": _T,
                "event_type": "daily_loss_block",
                "severity": "warning",
                "strategy_version_id": None,
                "symbol": "BTC/USDT",
                "details": {},
            },
        ],
    )
    resp = client.get("/trader/risk/status")
    assert resp.status_code == 200, resp.text
    assert resp.json()["kill_switch_tripped"] is False
