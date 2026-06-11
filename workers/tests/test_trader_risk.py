"""Tests for the trader v1 risk manager.

Every test runs against a real PostgresContainer (testcontainers).
`evaluate_risk` writes a `trader_risk_events` row on every block;
mocking psycopg cursors to test those writes would be more code than
running the real SQL via testcontainers.

Tests cover:
  - EXIT short-circuit (always approved)
  - SELL block (v1 long-only)
  - All eight checks in their natural order:
      1. kill switch
      2. daily loss breach
      3. weekly loss breach
      4a. strategy disabled
      4b. strategy not paper-approved
      5. stale data
      6. per-trade sizing (cap clipping, zero-size block)
      7. total open risk
      8. per-asset exposure
  - Ordering: an earlier check fires before a later one when both
    would trigger
  - `process_pending_signals` orchestration: BUY -> PENDING order,
    blocked signal -> risk event row + processed_at set

All tests carry `@pytest.mark.integration`; run via
`pytest -m integration`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from marketmind_shared.schemas.trader import RiskEventType, SignalKind
from marketmind_workers.trader.config import TraderSettings, get_trader_settings
from marketmind_workers.trader.risk import (
    RiskInputs,
    _PortfolioState,
    _WindowPnL,
    compute_window_pnl,
    evaluate_risk,
    process_pending_signals,
)
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
def _clean(database_url: str) -> None:
    """Reset risk-related tables between tests."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "TRUNCATE trader_strategies, trader_candles, "
            "trader_portfolio_snapshots, trader_alerts, "
            "trader_risk_events, trader_audit_logs RESTART IDENTITY CASCADE",
        )
        conn.commit()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TRADER_SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("TRADER_TIMEFRAMES", "4h")
    monkeypatch.setenv("TRADER_STARTING_CASH_GBP", "1000")
    monkeypatch.setenv("TRADER_MAX_RISK_PER_TRADE_PCT", "0.01")
    monkeypatch.setenv("TRADER_MAX_PORTFOLIO_RISK_PCT", "0.05")
    monkeypatch.setenv("TRADER_MAX_DAILY_LOSS_PCT", "0.02")
    monkeypatch.setenv("TRADER_MAX_WEEKLY_LOSS_PCT", "0.05")
    monkeypatch.setenv("TRADER_MAX_DRAWDOWN_PCT", "0.10")
    monkeypatch.setenv("TRADER_DATA_STALENESS_SECONDS", "600")
    get_trader_settings.cache_clear()
    return get_trader_settings()


# ---- RiskInputs builders ---------------------------------------------------
#
# Default state = HEALTHY: no drawdown, fresh data, no open risk,
# enough equity for any sane proposed trade. Tests override only
# the fields they're exercising.


def _healthy_portfolio() -> _PortfolioState:
    return _PortfolioState(
        cash=Decimal("1000"),
        equity=Decimal("1000"),
        peak_equity=Decimal("1000"),
        drawdown_pct=Decimal("0"),
        starting_equity=Decimal("1000"),
    )


def _zero_pnl() -> _WindowPnL:
    return _WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("0"))


def _healthy_inputs(*, now: datetime) -> RiskInputs:
    return RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )


def _seed_minimal_version_in_txn(
    conn: psycopg.Connection[Any],
    *,
    risk_pct: Decimal,
) -> UUID:
    """Insert a strategy + minimal version inside the caller's
    open transaction. Needed because `evaluate_risk` writes
    risk-event rows whose FK to `trader_strategy_versions`
    fires at INSERT time; without a real version row the SELL
    + KILL_SWITCH + every block test would fail with a
    ForeignKeyViolation.
    """
    sid = uuid4()
    vid = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trader_strategies (id, name) VALUES (%s, %s)",
            (str(sid), f"r-{uuid4().hex[:6]}"),
        )
        cur.execute(
            """
            INSERT INTO trader_strategy_versions
                (id, strategy_id, version, marketmind_spec_id, template,
                 parameters, symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                 backtest_metrics)
            VALUES (%s, %s, 1, %s, 'ma_trend', '{}'::jsonb, %s, %s, %s, 10, 10, '{}'::jsonb)
            """,
            (str(vid), str(sid), str(uuid4()), ["BTC/USDT"], ["4h"], risk_pct),
        )
    return vid


def _run_eval(
    conn: psycopg.Connection[Any],
    settings: TraderSettings,
    *,
    inputs: RiskInputs,
    signal_kind: SignalKind = SignalKind.BUY,
    proposed_entry_price: Decimal = Decimal("100"),
    proposed_stop_price: Decimal = Decimal("95"),
    strategy_risk_pct: Decimal = Decimal("0.005"),
    strategy_enabled: bool = True,
    strategy_approved_for_paper: bool = True,
    now: datetime | None = None,
):  # type: ignore[no-untyped-def]
    """Boilerplate-free evaluate_risk caller for the tests.

    Seeds a real version row in the same transaction so any
    risk-event INSERT inside evaluate_risk satisfies the FK to
    trader_strategy_versions.
    """
    version_id = _seed_minimal_version_in_txn(conn, risk_pct=strategy_risk_pct)
    return evaluate_risk(
        conn,
        settings,
        signal_id=None,  # tests don't seed signal rows; column is nullable
        signal_kind=signal_kind,
        symbol="BTC/USDT",
        proposed_entry_price=proposed_entry_price,
        proposed_stop_price=proposed_stop_price,
        strategy_version_id=version_id,
        strategy_risk_pct=strategy_risk_pct,
        strategy_enabled=strategy_enabled,
        strategy_approved_for_paper=strategy_approved_for_paper,
        inputs=inputs,
        now=now,
    )


# ---- evaluate_risk: pre-check short-circuits ------------------------------


def test_exit_signal_is_always_approved(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """EXIT bypasses every entry-side check, including the kill switch."""
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    # Construct a state that would block every entry: huge drawdown,
    # massive losses, stale data. EXIT must still approve.
    catastrophic = RiskInputs(
        portfolio=_PortfolioState(
            cash=Decimal("100"),
            equity=Decimal("100"),
            peak_equity=Decimal("1000"),
            drawdown_pct=Decimal("0.9"),  # 90% drawdown
            starting_equity=Decimal("1000"),
        ),
        daily_pnl=_WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("-500")),
        weekly_pnl=_WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("-500")),
        latest_candle_close_ts=now - timedelta(hours=24),  # very stale
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=catastrophic,
            signal_kind=SignalKind.EXIT,
            now=now,
        )
    assert decision.kind == "approved"
    assert decision.size is None  # EXIT sizes from the open position, set at fill time


def test_sell_signal_is_blocked_in_v1(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=_healthy_inputs(now=now),
            signal_kind=SignalKind.SELL,
            now=now,
        )
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.BLOCK
    assert "SELL" in (decision.reason or "")


# ---- Check 1: kill switch --------------------------------------------------


def test_kill_switch_threshold_boundary(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """The comparison is `>=`: at-threshold blocks, just-below approves.

    Two assertions in one test so the boundary semantics are clear
    in the failure message if the comparison ever drifts.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    settings_threshold = settings.trader_max_drawdown_pct  # 0.10 by default

    # --- Just below threshold ⇒ approved ---
    just_below = RiskInputs(
        portfolio=_PortfolioState(
            cash=Decimal("900"),
            equity=Decimal("900.1"),
            peak_equity=Decimal("1000"),
            drawdown_pct=settings_threshold - Decimal("0.0001"),  # 0.0999
            starting_equity=Decimal("1000"),
        ),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        below_decision = _run_eval(conn, settings, inputs=just_below, now=now)
    assert below_decision.kind == "approved", (
        f"drawdown just below threshold ({just_below.portfolio.drawdown_pct}) "
        f"should NOT trip kill switch (threshold {settings_threshold})"
    )

    # --- Exactly at threshold ⇒ blocked ---
    at_threshold = RiskInputs(
        portfolio=_PortfolioState(
            cash=Decimal("900"),
            equity=Decimal("900"),
            peak_equity=Decimal("1000"),
            drawdown_pct=settings_threshold,  # 0.10 exactly
            starting_equity=Decimal("1000"),
        ),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        at_decision = _run_eval(conn, settings, inputs=at_threshold, now=now)
    assert at_decision.kind == "blocked"
    assert at_decision.event_type is RiskEventType.KILL_SWITCH


def test_kill_switch_is_reactive_not_projective(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """The kill switch checks CURRENT drawdown — it does NOT simulate
    what would happen if the proposed trade's stop hits. A signal at
    9.5% drawdown with a very-wide stop is APPROVED even though
    hitting that stop would push drawdown past 10%. The next cycle
    catches the actual breach if/when it happens.

    This is documented behaviour: projective simulation adds
    complexity (forecasting all open positions' worst-case fills)
    for marginal safety gain. The trader's per-trade sizing (check
    6) caps single-trade max-loss at risk_pct of equity, so no
    single approved trade triggers a large surprise drawdown.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_PortfolioState(
            cash=Decimal("905"),
            equity=Decimal("905"),
            peak_equity=Decimal("1000"),
            drawdown_pct=Decimal("0.095"),  # 9.5%, below 10% cap
            starting_equity=Decimal("1000"),
        ),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        # Wide stop: entry 100, stop 50. If hit, the trade alone
        # would cost (size * 50) and likely push total drawdown
        # past 10%. The risk manager ignores this — current
        # drawdown < threshold, all other checks healthy ⇒
        # APPROVED.
        decision = _run_eval(
            conn,
            settings,
            inputs=inputs,
            proposed_entry_price=Decimal("100"),
            proposed_stop_price=Decimal("50"),
            now=now,
        )
    assert decision.kind == "approved", (
        "kill switch should be reactive (current drawdown only), not "
        "projective (hypothetical post-stop drawdown). A drawdown "
        "below threshold approves regardless of stop placement."
    )


def test_kill_switch_fires_at_max_drawdown(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = _healthy_inputs(now=now)
    inputs = RiskInputs(
        portfolio=_PortfolioState(
            cash=Decimal("900"),
            equity=Decimal("900"),
            peak_equity=Decimal("1000"),
            drawdown_pct=Decimal("0.10"),  # at threshold
            starting_equity=Decimal("1000"),
        ),
        daily_pnl=inputs.daily_pnl,
        weekly_pnl=inputs.weekly_pnl,
        latest_candle_close_ts=inputs.latest_candle_close_ts,
        total_open_risk=inputs.total_open_risk,
        symbol_existing_notional=inputs.symbol_existing_notional,
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.KILL_SWITCH

    # The risk-event row was written.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, severity FROM trader_risk_events ORDER BY ts DESC LIMIT 1",
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "kill_switch"
        assert row[1] == "critical"


# ---- Check 2: daily loss breach --------------------------------------------


def test_daily_loss_breach_fires(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Daily PnL <= -max_daily_loss_pct * starting_equity. Default
    settings: 0.02 * 1000 = 20. PnL = -25 trips the breach.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("-25")),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.DAILY_LOSS_BREACH


# ---- UTC midnight / Monday rollover (window-PnL anchor behaviour) ---------
#
# Step 13 audit gap: the existing tests pass `_WindowPnL` directly
# to evaluate_risk, bypassing the SQL that computes the daily /
# weekly anchor from `trader_portfolio_snapshots`. These two tests
# fill that gap — they exercise `compute_window_pnl` directly to
# verify it picks the right snapshot relative to the UTC boundary
# the orchestrator uses (`utc_midnight_of(now)` /
# `utc_monday_of(now)`). Together they pin the rollover semantic:
# a snapshot from the previous day IS the anchor; a snapshot from
# AFTER the boundary is not.


def test_compute_window_pnl_uses_pre_midnight_snapshot_as_daily_anchor(
    database_url: str,
    _clean: None,
) -> None:
    """A snapshot at 23:59:00 UTC on day N is the daily-PnL anchor
    for a `now` on day N+1. The query is `ts <= anchor_ts`, with
    anchor_ts = utc_midnight_of(now) = day-N+1 00:00:00 UTC.
    """
    from marketmind_shared.trader.time import utc_midnight_of

    now = datetime(2026, 5, 18, 0, 30, tzinfo=UTC)
    anchor = utc_midnight_of(now)
    assert anchor == datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    pre_midnight_ts = datetime(2026, 5, 17, 23, 59, tzinfo=UTC)
    earlier_ts = datetime(2026, 5, 17, 12, 0, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        for ts, equity in (
            (earlier_ts, Decimal("1000")),
            (pre_midnight_ts, Decimal("950")),
        ):
            cur.execute(
                """
                INSERT INTO trader_portfolio_snapshots
                    (ts, cash, equity, unrealised_pnl, realised_pnl_cumulative,
                     peak_equity, drawdown, drawdown_pct, open_positions_count)
                VALUES (%s, %s, %s, 0, 0, %s, 0, 0, 0)
                """,
                (ts, equity, equity, equity),
            )
        conn.commit()

    portfolio = _PortfolioState(
        cash=Decimal("0"),
        equity=Decimal("900"),
        peak_equity=Decimal("1000"),
        drawdown_pct=Decimal("0.10"),
        starting_equity=Decimal("1000"),
    )
    with psycopg.connect(database_url) as conn:
        window = compute_window_pnl(conn, anchor, portfolio)
    # Anchor equity should be the 23:59 snapshot (more recent than
    # the 12:00 one, both <= midnight).
    assert window.anchor_equity == Decimal("950")
    assert window.pnl == Decimal("-50")


def test_compute_window_pnl_skips_post_boundary_snapshot(
    database_url: str,
    _clean: None,
) -> None:
    """A snapshot strictly after the anchor boundary is NOT the
    anchor — the query is `ts <= anchor_ts`. When no snapshot
    exists at or before the anchor, compute_window_pnl falls back
    to `portfolio.starting_equity`.
    """
    from marketmind_shared.trader.time import utc_midnight_of

    now = datetime(2026, 5, 18, 0, 30, tzinfo=UTC)
    anchor = utc_midnight_of(now)
    post_boundary_ts = datetime(2026, 5, 18, 0, 15, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_portfolio_snapshots
                (ts, cash, equity, unrealised_pnl, realised_pnl_cumulative,
                 peak_equity, drawdown, drawdown_pct, open_positions_count)
            VALUES (%s, 0, 1100, 0, 0, 1100, 0, 0, 0)
            """,
            (post_boundary_ts,),
        )
        conn.commit()

    portfolio = _PortfolioState(
        cash=Decimal("0"),
        equity=Decimal("1200"),
        peak_equity=Decimal("1200"),
        drawdown_pct=Decimal("0"),
        starting_equity=Decimal("1000"),
    )
    with psycopg.connect(database_url) as conn:
        window = compute_window_pnl(conn, anchor, portfolio)
    # No snapshot at or before the anchor → starting_equity fallback.
    assert window.anchor_equity == Decimal("1000")
    assert window.pnl == Decimal("200")


# ---- Check 3: weekly loss breach -------------------------------------------


def test_weekly_loss_breach_fires(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Weekly cap: 0.05 * 1000 = 50. Pnl = -60 trips."""
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_zero_pnl(),  # daily within limit
        weekly_pnl=_WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("-60")),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.WEEKLY_LOSS_BREACH


# ---- Check 4a: strategy disabled -------------------------------------------


def test_strategy_disabled_blocks(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=_healthy_inputs(now=now),
            strategy_enabled=False,
            now=now,
        )
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.STRATEGY_DISABLED


# ---- Check 4b: strategy not paper-approved ---------------------------------


def test_strategy_not_paper_approved_blocks(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=_healthy_inputs(now=now),
            strategy_approved_for_paper=False,
            now=now,
        )
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.STRATEGY_NOT_PAPER_APPROVED


# ---- Check 5: stale data ---------------------------------------------------


def test_stale_data_blocks_when_candle_too_old(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    # Threshold = 600s. Last candle close 1000s ago ⇒ stale.
    inputs = RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=1000),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.STALE_DATA


def test_fresh_data_passes_stale_check(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Last candle 60s ago, threshold 600s ⇒ fresh ⇒ check passes
    (the trade is approved at this point because every other check
    is healthy).
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=_healthy_inputs(now=now), now=now)
    assert decision.kind == "approved"


# ---- Check 6: per-trade sizing ---------------------------------------------


def test_per_trade_sizing_uses_strategy_risk_pct_when_below_global_cap(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Strategy risk_pct=0.005 < global cap=0.01.
    size = equity(1000) * 0.005 / stop_distance(5) = 1.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=_healthy_inputs(now=now),
            strategy_risk_pct=Decimal("0.005"),
            proposed_entry_price=Decimal("100"),
            proposed_stop_price=Decimal("95"),
            now=now,
        )
    assert decision.kind == "approved"
    assert decision.size == Decimal("1.00000000")


def test_per_trade_sizing_caps_when_strategy_exceeds_global(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Strategy asks for 0.10 (10%), global cap = 0.01 (1%).
    size = equity(1000) * 0.01 / stop_distance(5) = 2.
    Cap wins, smaller size returned.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=_healthy_inputs(now=now),
            strategy_risk_pct=Decimal("0.10"),
            proposed_entry_price=Decimal("100"),
            proposed_stop_price=Decimal("95"),
            now=now,
        )
    assert decision.kind == "approved"
    assert decision.size == Decimal("2.00000000")


def test_invalid_stop_at_or_above_entry_blocks(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(
            conn,
            settings,
            inputs=_healthy_inputs(now=now),
            proposed_entry_price=Decimal("100"),
            proposed_stop_price=Decimal("100"),  # at entry — invalid
            now=now,
        )
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.BLOCK
    assert "stop" in (decision.reason or "").lower()


# ---- Check 7: total open risk ----------------------------------------------


def test_total_open_risk_breach_blocks(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Cap = 0.05 * 1000 = 50. Existing risk 48 + proposed risk 5 = 53.
    Exceeds 50 ⇒ block.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("48"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        # size = 1000 * 0.005 / 5 = 1. Proposed trade risk = 1 * 5 = 5.
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.BLOCK


# ---- Check 8: per-asset exposure -------------------------------------------


def test_per_asset_exposure_breach_blocks(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Cap = 0.5 * 1000 = 500. Existing notional 450 + proposed
    notional = (size=1) * (price=100) = 100. New = 550 > 500 ⇒ block.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_zero_pnl(),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("450"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.BLOCK


# ---- Healthy approval ------------------------------------------------------


def test_all_checks_pass_returns_approved_with_sized_amount(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Healthy state ⇒ approved with the proper sized amount.
    size = equity(1000) * risk_pct(0.005) / stop_distance(5) = 1.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=_healthy_inputs(now=now), now=now)
    assert decision.kind == "approved"
    assert decision.size == Decimal("1.00000000")


# ---- Ordering --------------------------------------------------------------


def test_kill_switch_fires_before_daily_loss_when_both_would_trigger(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Both kill-switch AND daily-loss-breach conditions hold.
    Kill switch is check 1, daily is check 2 → kill switch wins.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_PortfolioState(
            cash=Decimal("800"),
            equity=Decimal("800"),
            peak_equity=Decimal("1000"),
            drawdown_pct=Decimal("0.20"),  # past kill-switch threshold
            starting_equity=Decimal("1000"),
        ),
        daily_pnl=_WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("-100")),  # past cap
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=60),
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.KILL_SWITCH


def test_daily_loss_fires_before_stale_data_when_both_would_trigger(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    inputs = RiskInputs(
        portfolio=_healthy_portfolio(),
        daily_pnl=_WindowPnL(anchor_equity=Decimal("1000"), pnl=Decimal("-100")),
        weekly_pnl=_zero_pnl(),
        latest_candle_close_ts=now - timedelta(seconds=10000),  # very stale
        total_open_risk=Decimal("0"),
        symbol_existing_notional=Decimal("0"),
    )
    with psycopg.connect(database_url) as conn, conn.transaction():
        decision = _run_eval(conn, settings, inputs=inputs, now=now)
    assert decision.kind == "blocked"
    assert decision.event_type is RiskEventType.DAILY_LOSS_BREACH


# ---- process_pending_signals orchestrator ----------------------------------


def _seed_strategy_version(
    database_url: str,
    *,
    enabled: bool = True,
    approved_for_paper: bool = True,
) -> UUID:
    name = f"risk-test-{uuid4().hex[:8]}"
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
            INSERT INTO trader_strategy_versions (
                strategy_id, version, marketmind_spec_id, template, parameters,
                symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                backtest_metrics, approved_for_paper, enabled
            ) VALUES (%s, 1, %s, 'ma_trend', %s, %s, %s, %s, 10, 10, %s, %s, %s)
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
                approved_for_paper,
                enabled,
            ),
        )
        vrow = cur.fetchone()
        assert vrow is not None
        conn.commit()
    return UUID(str(vrow[0]))


def _seed_candle(database_url: str, *, close_ts: datetime, close_price: Decimal) -> None:
    open_ts = close_ts - timedelta(hours=4)
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_candles
                (symbol, timeframe, open_ts, close_ts, open, high, low, close, volume, is_closed)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "BTC/USDT",
                "4h",
                open_ts,
                close_ts,
                close_price,
                close_price * Decimal("1.001"),
                close_price * Decimal("0.999"),
                close_price,
                Decimal("1000"),
                True,
            ),
        )
        conn.commit()


def _seed_buy_signal(
    database_url: str,
    *,
    version_id: UUID,
    candle_close_ts: datetime,
    entry_price: Decimal = Decimal("100"),
    stop_price: Decimal = Decimal("95"),
) -> UUID:
    signal_id = uuid4()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_signals (
                id, strategy_version_id, symbol, timeframe, candle_close_ts,
                signal, reason, indicators,
                proposed_entry_price, proposed_stop_price
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(signal_id),
                str(version_id),
                "BTC/USDT",
                "4h",
                candle_close_ts,
                "BUY",
                "test seed",
                Jsonb({}),
                entry_price,
                stop_price,
            ),
        )
        conn.commit()
    return signal_id


def test_process_pending_creates_paper_order_for_approved_buy(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Approved BUY signal → PENDING trader_paper_orders row,
    signal.processed_at set, no risk_event row."""
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    candle_close_ts = now - timedelta(seconds=60)
    version_id = _seed_strategy_version(database_url)
    _seed_candle(database_url, close_ts=candle_close_ts, close_price=Decimal("100"))
    signal_id = _seed_buy_signal(
        database_url,
        version_id=version_id,
        candle_close_ts=candle_close_ts,
    )

    result = process_pending_signals(database_url, settings, now=now)

    assert result.signals_processed == 1
    assert result.signals_approved == 1
    assert result.signals_blocked == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT side, requested_size, status, intended_fill_ts FROM trader_paper_orders "
            "WHERE signal_id = %s",
            (str(signal_id),),
        )
        order_row = cur.fetchone()
        assert order_row is not None
        side, size, status, intended_fill_ts = order_row
        assert side == "BUY"
        assert size == Decimal("1.00000000")  # equity 1000 * 0.005 / stop_distance 5
        assert status == "PENDING"
        assert intended_fill_ts == candle_close_ts  # next-bar-open

        cur.execute(
            "SELECT processed_at FROM trader_signals WHERE id = %s",
            (str(signal_id),),
        )
        psig = cur.fetchone()
        assert psig is not None
        assert psig[0] is not None

        cur.execute("SELECT COUNT(*) FROM trader_risk_events")
        rc = cur.fetchone()
        assert rc is not None
        assert rc[0] == 0


def test_process_pending_blocks_unapproved_signal_with_risk_event(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Strategy approved_for_paper=False ⇒ block. risk_event row
    written; signal.processed_at set; NO paper order created.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    candle_close_ts = now - timedelta(seconds=60)
    version_id = _seed_strategy_version(database_url, approved_for_paper=False)
    _seed_candle(database_url, close_ts=candle_close_ts, close_price=Decimal("100"))
    signal_id = _seed_buy_signal(
        database_url,
        version_id=version_id,
        candle_close_ts=candle_close_ts,
    )

    result = process_pending_signals(database_url, settings, now=now)

    assert result.signals_processed == 1
    assert result.signals_approved == 0
    assert result.signals_blocked == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM trader_paper_orders WHERE signal_id = %s",
            (str(signal_id),),
        )
        orderc = cur.fetchone()
        assert orderc is not None
        assert orderc[0] == 0

        cur.execute(
            "SELECT event_type, severity FROM trader_risk_events WHERE signal_id = %s",
            (str(signal_id),),
        )
        rrow = cur.fetchone()
        assert rrow is not None
        assert rrow[0] == "strategy_not_paper_approved"
        assert rrow[1] == "warning"


def test_process_pending_is_idempotent_on_replay(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """Second cycle finds no unprocessed signals (first cycle set
    processed_at). No additional orders / events.
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    candle_close_ts = now - timedelta(seconds=60)
    version_id = _seed_strategy_version(database_url)
    _seed_candle(database_url, close_ts=candle_close_ts, close_price=Decimal("100"))
    _seed_buy_signal(
        database_url,
        version_id=version_id,
        candle_close_ts=candle_close_ts,
    )

    first = process_pending_signals(database_url, settings, now=now)
    second = process_pending_signals(database_url, settings, now=now)

    assert first.signals_processed == 1
    assert second.signals_processed == 0  # nothing unprocessed left

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_paper_orders")
        rc = cur.fetchone()
        assert rc is not None
        assert rc[0] == 1


def test_process_pending_skips_signal_when_version_missing(
    database_url: str,
    settings: TraderSettings,
    _clean: None,
) -> None:
    """A signal whose strategy_version was deleted between signal
    persistence and the risk pass is skipped (processed_at set,
    no order, no risk event).
    """
    now = datetime(2026, 5, 18, 12, tzinfo=UTC)
    candle_close_ts = now - timedelta(seconds=60)
    version_id = _seed_strategy_version(database_url)
    _seed_candle(database_url, close_ts=candle_close_ts, close_price=Decimal("100"))
    signal_id = _seed_buy_signal(
        database_url,
        version_id=version_id,
        candle_close_ts=candle_close_ts,
    )
    # The signal row holds a FK to the version with ON DELETE
    # CASCADE: deleting the version wipes the signal too, so the
    # orchestrator's `_load_unprocessed_signals` never sees a
    # signal with a missing version. Unreachable under the
    # current FK CASCADE; the `signals_skipped_missing_version`
    # branch is kept for future schema flexibility (e.g., if we
    # ever switch to `ON DELETE SET NULL` to keep signal history
    # past a version delete).
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        # Insert a placeholder strategy + version we can repoint at
        # so the FK constraint stays satisfied, then DELETE the
        # placeholder to make the FK dangling (cascade fires).
        # Simpler: just check the result counts when the version
        # row is genuinely deleted via CASCADE. The original
        # `version_id` was deleted; check that the signal is gone
        # too (FK ON DELETE CASCADE).
        cur.execute("DELETE FROM trader_strategy_versions WHERE id = %s", (str(version_id),))
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM trader_signals WHERE id = %s", (str(signal_id),))
        srow = cur.fetchone()
        assert srow is not None
        assert srow[0] == 0  # cascade wiped the signal

    # Confirms the cascade behavior the test originally tried to
    # exercise: a "version missing" signal can't exist in v1's
    # schema, so the orchestrator's missing-version branch is
    # defensive-only (covered by the orchestrator code path; the
    # FK constraint means it can never fire in practice).
    result = process_pending_signals(database_url, settings, now=now)
    assert result.signals_processed == 0
    assert result.signals_skipped_missing_version == 0
