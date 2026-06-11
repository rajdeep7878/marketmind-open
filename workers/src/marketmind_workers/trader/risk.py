"""Trader v1 risk manager.

`evaluate_risk(conn, settings, signal, version, context)` is the
gatekeeper: every signal must pass it before an order is created.
Applies the eight v1 risk checks in the prompt's fixed order and
short-circuits on the first block. Each block writes a
`trader_risk_events` row in the caller's transaction and returns
a `BlockDecision(kind='blocked', ...)`.

`process_pending_signals(database_url, settings, ...)` is the
orchestrator the signal-execution loop calls between the signal
engine and the executor. It loads unprocessed signals, computes
the portfolio + window-PnL state once per signal (state changes
when prior approvals create new orders), calls `evaluate_risk`,
and persists either a paper order (approved) or a risk-event row
(blocked). Either way, the signal's `processed_at` is set so the
next cycle doesn't re-evaluate.

CHECK ORDER (load-bearing — short-circuit on first block):
  1. KILL_SWITCH                drawdown >= max_drawdown_pct
  2. DAILY_LOSS_BREACH          today's PnL <= -daily_loss_cap
  3. WEEKLY_LOSS_BREACH         week's PnL <= -weekly_loss_cap
  4. STRATEGY_DISABLED          version.enabled == False (defensive)
  4. STRATEGY_NOT_PAPER_APPROVED version.approved_for_paper == False
  5. STALE_DATA                 now - latest_candle_close_ts > threshold
  6. (per-trade size cap)       sizes the trade; blocks if min size <= 0
  7. (total open risk)          new total risk > max_portfolio_risk_pct
  8. (per-asset exposure)       symbol notional > 50% of equity

EXIT signals BYPASS all entry-side checks (the prompt's
"always allowed to close" rule). SELL signals are blocked in v1
(long-only spot; the templates never emit SELL — a SELL signal
indicates upstream corruption).

The kill-switch event is emitted with severity=critical; the
six "block" categories use warning. Step 10's alert dispatcher
turns critical/warning into Telegram messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid4

import psycopg
import structlog
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.trader import (
    BlockDecision,
    RiskEventType,
    Severity,
    SignalKind,
)
from marketmind_shared.trader.money import quantize_size, to_decimal
from marketmind_shared.trader.time import (
    now_utc,
    utc_midnight_of,
    utc_monday_of,
)
from psycopg.types.json import Jsonb

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.heartbeat import touch_heartbeat

log = structlog.get_logger(__name__)


# Per-symbol exposure cap: a single symbol's open notional can't
# exceed this fraction of equity. Per the prompt's check 8.
_PER_SYMBOL_EXPOSURE_CAP_PCT: Final[Decimal] = Decimal("0.5")


# ---- Value types -----------------------------------------------------------


@dataclass(frozen=True)
class _PortfolioState:
    """Snapshot of trader-wide state needed for risk checks."""

    cash: Decimal
    equity: Decimal  # cash + sum(MTM of open positions)
    peak_equity: Decimal
    drawdown_pct: Decimal  # (peak - equity) / peak, or 0 if peak==0
    starting_equity: Decimal


@dataclass(frozen=True)
class _WindowPnL:
    """PnL since a window anchor (UTC midnight for daily, UTC Monday
    for weekly). Computed as (current_equity - anchor_equity); the
    anchor equity is the most recent snapshot at or before the
    anchor instant, falling back to starting_equity if no snapshot
    is older than the anchor.
    """

    anchor_equity: Decimal
    pnl: Decimal  # current_equity - anchor_equity (sign convention: gain positive)


@dataclass(frozen=True)
class RiskInputs:
    """All inputs `evaluate_risk` needs for one signal.

    Public type so call sites (Step 12 runner) can compose it from
    DB reads + settings; tests construct it directly with synthetic
    values to drive each check independently.
    """

    portfolio: _PortfolioState
    daily_pnl: _WindowPnL
    weekly_pnl: _WindowPnL
    latest_candle_close_ts: datetime
    # Sum across all CURRENTLY-open paper positions:
    # size * abs(entry_price - stop_price). Excludes the proposed
    # trade (whose risk is added in the check itself).
    total_open_risk: Decimal
    # Sum of size * latest_close_price for open positions on the
    # SAME symbol as the proposed trade. Excludes the proposed
    # trade.
    symbol_existing_notional: Decimal


# ---- Pure-function check kernel --------------------------------------------


def evaluate_risk(
    conn: psycopg.Connection[Any],
    settings: TraderSettings,
    *,
    signal_id: UUID | None,
    signal_kind: SignalKind,
    symbol: str,
    proposed_entry_price: Decimal,
    proposed_stop_price: Decimal,
    strategy_version_id: UUID,
    strategy_risk_pct: Decimal,
    strategy_enabled: bool,
    strategy_approved_for_paper: bool,
    inputs: RiskInputs,
    now: datetime | None = None,
) -> BlockDecision:
    """Apply the 8 v1 risk checks in fixed order; first block wins.

    EXIT signals bypass entry-side checks (always allowed to close).
    SELL signals are blocked in v1 (long-only spot; SELL implies
    short-side which isn't supported).

    On block, writes a `trader_risk_events` row in the caller's
    transaction. The caller is responsible for committing.

    `signal_id` is Optional because tests sometimes evaluate risk
    in isolation without a persisted signal row; in those cases the
    risk_events row records `signal_id = NULL` (the column is
    nullable, with `ON DELETE SET NULL`). Real call sites
    (`process_pending_signals`) always pass a real signal_id.
    """
    actual_now = now if now is not None else now_utc()

    # ---- EXIT short-circuit -------------------------------------------------
    if signal_kind is SignalKind.EXIT:
        # Exits close existing positions and are always allowed —
        # even when the kill switch is tripped. The executor sizes
        # at the open position's size, not at a fresh proposed_size.
        return BlockDecision(kind="approved", size=None)

    # ---- SELL is unsupported in v1 ------------------------------------------
    if signal_kind is SignalKind.SELL:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.BLOCK,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "reason": "sell_not_supported_in_v1",
                "signal_kind": signal_kind.value,
            },
        )
        return BlockDecision(
            kind="blocked",
            reason="SELL signal: v1 is long-only spot; SELL is not supported",
            event_type=RiskEventType.BLOCK,
            risk_event_id=event_id,
        )

    # HOLD never reaches here — the signal engine doesn't persist HOLDs.
    # BUY is the only kind that flows through the entry-side checks below.

    # ---- 1. Kill switch ------------------------------------------------------
    #
    # REACTIVE, not projective: checks CURRENT drawdown only. A BUY
    # whose stop, if hit, would push drawdown past the threshold is
    # APPROVED — projection is not the risk manager's job (the next
    # cycle's check fires if the actual breach happens). Conservative
    # per-trade sizing (check 6) caps the single-trade max-loss at
    # risk_pct of equity, so a single approved trade can't trigger a
    # large surprise drawdown.
    #
    # Comparison is `>=`: drawdown EXACTLY equal to the threshold
    # blocks. Standard convention; the boundary is verified by
    # test_kill_switch_threshold_boundary in test_trader_risk.py.
    if inputs.portfolio.drawdown_pct >= settings.trader_max_drawdown_pct:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.KILL_SWITCH,
            Severity.CRITICAL,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "drawdown_pct": str(inputs.portfolio.drawdown_pct),
                "max_drawdown_pct": str(settings.trader_max_drawdown_pct),
                "equity": str(inputs.portfolio.equity),
                "peak_equity": str(inputs.portfolio.peak_equity),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason=(
                f"kill switch: drawdown {inputs.portfolio.drawdown_pct:.4f} >= "
                f"max {settings.trader_max_drawdown_pct}"
            ),
            event_type=RiskEventType.KILL_SWITCH,
            risk_event_id=event_id,
        )

    # ---- 2. Daily loss breach ------------------------------------------------
    daily_loss_cap = settings.trader_max_daily_loss_pct * inputs.portfolio.starting_equity
    if inputs.daily_pnl.pnl <= -daily_loss_cap:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.DAILY_LOSS_BREACH,
            Severity.CRITICAL,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "daily_pnl": str(inputs.daily_pnl.pnl),
                "daily_loss_cap": str(-daily_loss_cap),
                "anchor_equity": str(inputs.daily_pnl.anchor_equity),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason=f"daily loss breach: pnl {inputs.daily_pnl.pnl} <= -{daily_loss_cap}",
            event_type=RiskEventType.DAILY_LOSS_BREACH,
            risk_event_id=event_id,
        )

    # ---- 3. Weekly loss breach -----------------------------------------------
    weekly_loss_cap = settings.trader_max_weekly_loss_pct * inputs.portfolio.starting_equity
    if inputs.weekly_pnl.pnl <= -weekly_loss_cap:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.WEEKLY_LOSS_BREACH,
            Severity.CRITICAL,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "weekly_pnl": str(inputs.weekly_pnl.pnl),
                "weekly_loss_cap": str(-weekly_loss_cap),
                "anchor_equity": str(inputs.weekly_pnl.anchor_equity),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason=f"weekly loss breach: pnl {inputs.weekly_pnl.pnl} <= -{weekly_loss_cap}",
            event_type=RiskEventType.WEEKLY_LOSS_BREACH,
            risk_event_id=event_id,
        )

    # ---- 4a. Strategy disabled (defensive) ----------------------------------
    if not strategy_enabled:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.STRATEGY_DISABLED,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={"reason": "strategy_version.enabled is False"},
        )
        return BlockDecision(
            kind="blocked",
            reason="strategy version is disabled",
            event_type=RiskEventType.STRATEGY_DISABLED,
            risk_event_id=event_id,
        )

    # ---- 4b. Strategy not paper-approved (defensive) ------------------------
    if not strategy_approved_for_paper:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.STRATEGY_NOT_PAPER_APPROVED,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={"reason": "strategy_version.approved_for_paper is False"},
        )
        return BlockDecision(
            kind="blocked",
            reason="strategy version is not paper-approved",
            event_type=RiskEventType.STRATEGY_NOT_PAPER_APPROVED,
            risk_event_id=event_id,
        )

    # ---- 5. Data stale -------------------------------------------------------
    staleness = actual_now - inputs.latest_candle_close_ts
    if staleness > timedelta(seconds=settings.trader_data_staleness_seconds):
        event_id = _emit_risk_event(
            conn,
            RiskEventType.STALE_DATA,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "latest_close_ts": inputs.latest_candle_close_ts.isoformat(),
                "now": actual_now.isoformat(),
                "staleness_seconds": staleness.total_seconds(),
                "threshold_seconds": settings.trader_data_staleness_seconds,
            },
        )
        return BlockDecision(
            kind="blocked",
            reason=(
                f"stale data: latest candle close {staleness.total_seconds():.0f}s old "
                f"(threshold {settings.trader_data_staleness_seconds}s)"
            ),
            event_type=RiskEventType.STALE_DATA,
            risk_event_id=event_id,
        )

    # ---- 6. Per-trade risk + sizing -----------------------------------------
    stop_distance = proposed_entry_price - proposed_stop_price
    if stop_distance <= Decimal(0):
        # Defensive: BUY signals must carry a stop BELOW entry.
        # Step 3 templates + Step 5 orchestrator both validate this,
        # but the runtime guard is the safety net.
        event_id = _emit_risk_event(
            conn,
            RiskEventType.BLOCK,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "reason": "stop_at_or_above_entry",
                "entry": str(proposed_entry_price),
                "stop": str(proposed_stop_price),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason="invalid stop: stop_price must be strictly below entry_price for a long",
            event_type=RiskEventType.BLOCK,
            risk_event_id=event_id,
        )

    # Effective risk pct = min(strategy.risk_pct, global cap).
    # If the strategy asks for more risk than the global cap allows,
    # the cap wins (size adjusts down).
    effective_risk_pct = min(strategy_risk_pct, settings.trader_max_risk_per_trade_pct)
    proposed_size_raw = (inputs.portfolio.equity * effective_risk_pct) / stop_distance
    proposed_size = quantize_size(proposed_size_raw)

    if proposed_size <= Decimal(0):
        # Size quantised to zero — impossibly small risk budget for
        # this stop distance + equity. Block until the operator
        # increases equity or the strategy revisits its risk_pct.
        event_id = _emit_risk_event(
            conn,
            RiskEventType.BLOCK,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "reason": "size_quantised_to_zero",
                "equity": str(inputs.portfolio.equity),
                "effective_risk_pct": str(effective_risk_pct),
                "stop_distance": str(stop_distance),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason="per-trade risk: size quantised to zero (equity * risk_pct too small)",
            event_type=RiskEventType.BLOCK,
            risk_event_id=event_id,
        )

    # ---- 7. Total open risk --------------------------------------------------
    proposed_trade_risk = proposed_size * stop_distance
    new_total_risk = inputs.total_open_risk + proposed_trade_risk
    portfolio_risk_cap = settings.trader_max_portfolio_risk_pct * inputs.portfolio.equity
    if new_total_risk > portfolio_risk_cap:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.BLOCK,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "reason": "total_open_risk_breach",
                "existing_open_risk": str(inputs.total_open_risk),
                "proposed_trade_risk": str(proposed_trade_risk),
                "new_total_risk": str(new_total_risk),
                "portfolio_risk_cap": str(portfolio_risk_cap),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason=(
                f"total open risk: new {new_total_risk} > cap {portfolio_risk_cap} "
                f"(equity {inputs.portfolio.equity} * "
                f"{settings.trader_max_portfolio_risk_pct})"
            ),
            event_type=RiskEventType.BLOCK,
            risk_event_id=event_id,
        )

    # ---- 8. Per-asset exposure cap ------------------------------------------
    proposed_trade_notional = proposed_size * proposed_entry_price
    new_symbol_notional = inputs.symbol_existing_notional + proposed_trade_notional
    symbol_notional_cap = _PER_SYMBOL_EXPOSURE_CAP_PCT * inputs.portfolio.equity
    if new_symbol_notional > symbol_notional_cap:
        event_id = _emit_risk_event(
            conn,
            RiskEventType.BLOCK,
            Severity.WARNING,
            strategy_version_id=strategy_version_id,
            symbol=symbol,
            signal_id=signal_id,
            details={
                "reason": "per_symbol_exposure_breach",
                "existing_symbol_notional": str(inputs.symbol_existing_notional),
                "proposed_trade_notional": str(proposed_trade_notional),
                "new_symbol_notional": str(new_symbol_notional),
                "symbol_notional_cap": str(symbol_notional_cap),
            },
        )
        return BlockDecision(
            kind="blocked",
            reason=(
                f"per-symbol exposure: new {new_symbol_notional} > cap "
                f"{symbol_notional_cap} (50% of equity)"
            ),
            event_type=RiskEventType.BLOCK,
            risk_event_id=event_id,
        )

    # All checks passed.
    return BlockDecision(kind="approved", size=proposed_size)


# ---- DB-touching helpers ---------------------------------------------------


def _emit_risk_event(
    conn: psycopg.Connection[Any],
    event_type: RiskEventType,
    severity: Severity,
    *,
    strategy_version_id: UUID | None = None,
    symbol: str | None = None,
    signal_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> UUID:
    """INSERT a trader_risk_events row. Returns the row's id.

    Called from inside `evaluate_risk` on every block; the caller's
    transaction commits the event alongside the signal's
    processed_at marker.
    """
    payload = details if details is not None else {}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_risk_events
                (event_type, severity, strategy_version_id, symbol, signal_id, details)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                event_type.value,
                severity.value,
                str(strategy_version_id) if strategy_version_id is not None else None,
                symbol,
                str(signal_id) if signal_id is not None else None,
                Jsonb(payload),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return UUID(str(row[0]))


def compute_portfolio_state(
    conn: psycopg.Connection[Any],
    settings: TraderSettings,
) -> _PortfolioState:
    """Compute current trader-wide state from the DB.

    v1 strategy:
      - The latest `trader_portfolio_snapshots` row is the source of
        truth for cash + equity + peak. Step 8 writes a snapshot
        every signal-execution cycle.
      - If no snapshot exists yet (cold start), fall back to:
          cash = settings.trader_starting_cash_gbp
          equity = cash (no positions to MTM)
          peak_equity = cash
          drawdown_pct = 0
        This branch is only taken on the very first cycle; once
        Step 8 lands, every subsequent cycle has a snapshot.
    """
    starting_equity = to_decimal(settings.trader_starting_cash_gbp)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cash, equity, peak_equity, drawdown_pct
            FROM trader_portfolio_snapshots
            ORDER BY ts DESC
            LIMIT 1
            """,
        )
        row = cur.fetchone()
    if row is None:
        return _PortfolioState(
            cash=starting_equity,
            equity=starting_equity,
            peak_equity=starting_equity,
            drawdown_pct=Decimal(0),
            starting_equity=starting_equity,
        )
    return _PortfolioState(
        cash=row[0],
        equity=row[1],
        peak_equity=row[2],
        drawdown_pct=row[3],
        starting_equity=starting_equity,
    )


def compute_window_pnl(
    conn: psycopg.Connection[Any],
    anchor_ts: datetime,
    portfolio: _PortfolioState,
) -> _WindowPnL:
    """PnL since `anchor_ts`: current equity minus the snapshot's
    equity at or before the anchor.

    Falls back to `starting_equity` when no snapshot is older than
    the anchor — e.g., a brand-new bot run on its first day. In that
    case pnl == current - starting, which is the right answer.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT equity FROM trader_portfolio_snapshots
            WHERE ts <= %s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (anchor_ts,),
        )
        row = cur.fetchone()
    anchor_equity = row[0] if row is not None else portfolio.starting_equity
    return _WindowPnL(
        anchor_equity=anchor_equity,
        pnl=portfolio.equity - anchor_equity,
    )


def compute_total_open_risk(conn: psycopg.Connection[Any]) -> Decimal:
    """Sum across all OPEN positions: size * abs(entry - stop)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(size * ABS(entry_price - stop_price)), 0)
            FROM trader_paper_positions
            WHERE status = 'OPEN'
            """,
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return Decimal(0)
    return to_decimal(row[0])


def compute_symbol_existing_notional(
    conn: psycopg.Connection[Any],
    symbol: str,
) -> Decimal:
    """Sum of `size * latest_close` for OPEN positions on `symbol`.

    Latest close is the most recent `trader_candles.close` for the
    symbol (across timeframes — picks the latest by close_ts). This
    approximates mark-to-market without snapshotting every cycle.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
                SELECT close FROM trader_candles
                WHERE symbol = %s AND is_closed = TRUE
                ORDER BY close_ts DESC
                LIMIT 1
            )
            SELECT COALESCE(SUM(p.size * (SELECT close FROM latest)), 0)
            FROM trader_paper_positions p
            WHERE p.symbol = %s AND p.status = 'OPEN'
            """,
            (symbol, symbol),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return Decimal(0)
    return to_decimal(row[0])


def latest_candle_close_ts(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
) -> datetime | None:
    """Return the close_ts of the latest closed candle for the pair."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT close_ts FROM trader_candles
            WHERE symbol = %s AND timeframe = %s AND is_closed = TRUE
            ORDER BY close_ts DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0]


# ---- Orchestrator ----------------------------------------------------------


class RiskCycleResult(_StrictModel):
    """Aggregate stats from one risk-manager pass."""

    signals_processed: int = 0
    signals_approved: int = 0
    signals_blocked: int = 0
    signals_skipped_no_data: int = 0  # no candle for stale-data check
    signals_skipped_missing_version: int = 0  # version row missing / deleted


@dataclass
class _CycleState:
    signals_processed: int = 0
    signals_approved: int = 0
    signals_blocked: int = 0
    signals_skipped_no_data: int = 0
    signals_skipped_missing_version: int = 0


def _load_unprocessed_signals(
    conn: psycopg.Connection[Any],
) -> list[tuple[Any, ...]]:
    """Pull every unprocessed non-HOLD signal, oldest first.

    Order by `candle_close_ts` ascending so a backlog catches up in
    chronological order — important for the portfolio state to
    update correctly across consecutive approvals.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, strategy_version_id, symbol, timeframe,
                   candle_close_ts, signal,
                   proposed_entry_price, proposed_stop_price
            FROM trader_signals
            WHERE processed_at IS NULL AND signal != 'HOLD'
            ORDER BY candle_close_ts, created_at
            """,
        )
        return cur.fetchall()


def _load_version_for_risk(
    conn: psycopg.Connection[Any],
    version_id: UUID,
) -> tuple[Decimal, bool, bool] | None:
    """Return `(risk_pct, enabled, approved_for_paper)` for the
    version. None if the row is missing (operator deleted it
    between the signal write and now).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT risk_pct, enabled, approved_for_paper
            FROM trader_strategy_versions
            WHERE id = %s
            """,
            (str(version_id),),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2]


def _create_paper_order(
    conn: psycopg.Connection[Any],
    *,
    signal_id: UUID,
    strategy_version_id: UUID,
    symbol: str,
    side: str,
    size: Decimal,
    intended_fill_ts: datetime,
) -> UUID:
    """Insert a PENDING paper order. Returns the order id.

    The unique constraint on `signal_id` enforces one order per
    signal — calling this twice for the same signal would raise.
    """
    order_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side,
                 order_type, requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(order_id),
                str(signal_id),
                str(strategy_version_id),
                symbol,
                side,
                "MARKET",
                size,
                "PENDING",
                intended_fill_ts,
            ),
        )
    return order_id


def _mark_signal_processed(
    conn: psycopg.Connection[Any],
    signal_id: UUID,
) -> None:
    """Set processed_at = NOW() on the signal row. After this, the
    next cycle won't pick it up via _load_unprocessed_signals.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trader_signals SET processed_at = NOW() WHERE id = %s",
            (str(signal_id),),
        )


def process_pending_signals(
    database_url: str,
    settings: TraderSettings,
    *,
    now: datetime | None = None,
    run_id: UUID | None = None,
) -> RiskCycleResult:
    """Run one risk-manager pass.

    Pulls every unprocessed non-HOLD signal, evaluates it through
    `evaluate_risk`, and persists either:
      - A `trader_paper_orders` row (PENDING) for approved entries.
        EXITs flow through here too with size=None — the executor
        sizes EXIT orders from the open position itself in Step 7.
      - A `trader_risk_events` row (written inside evaluate_risk)
        for blocks.

    In both cases, `trader_signals.processed_at` is set so the next
    cycle doesn't re-evaluate. State (portfolio, window-PnL, total
    open risk, symbol notional) is recomputed PER SIGNAL so a chain
    of approvals correctly accounts for the orders queued by earlier
    iterations.
    """
    state = _CycleState()
    actual_now = now if now is not None else now_utc()

    with psycopg.connect(database_url) as conn:
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="risk")
        signal_rows = _load_unprocessed_signals(conn)
        log.info("risk_cycle_starting", pending_signals=len(signal_rows))

        for row in signal_rows:
            (
                signal_id,
                version_id,
                symbol,
                timeframe,
                candle_close_ts,
                signal_kind_str,
                proposed_entry_price,
                proposed_stop_price,
            ) = row
            signal_id = UUID(str(signal_id))
            version_id = UUID(str(version_id))
            kind = SignalKind(signal_kind_str)

            state.signals_processed += 1
            with conn.transaction():
                version_data = _load_version_for_risk(conn, version_id)
                if version_data is None:
                    state.signals_skipped_missing_version += 1
                    _mark_signal_processed(conn, signal_id)
                    log.warning(
                        "risk_skip_missing_version",
                        signal_id=str(signal_id),
                        version_id=str(version_id),
                    )
                    continue
                risk_pct, enabled, approved_for_paper = version_data

                latest_close = latest_candle_close_ts(conn, symbol, timeframe)
                if latest_close is None:
                    state.signals_skipped_no_data += 1
                    _mark_signal_processed(conn, signal_id)
                    log.warning(
                        "risk_skip_no_candle",
                        signal_id=str(signal_id),
                        symbol=symbol,
                        timeframe=timeframe,
                    )
                    continue

                portfolio = compute_portfolio_state(conn, settings)
                daily_pnl = compute_window_pnl(
                    conn,
                    utc_midnight_of(actual_now),
                    portfolio,
                )
                weekly_pnl = compute_window_pnl(
                    conn,
                    utc_monday_of(actual_now),
                    portfolio,
                )
                total_open_risk = compute_total_open_risk(conn)
                symbol_notional = compute_symbol_existing_notional(conn, symbol)

                inputs = RiskInputs(
                    portfolio=portfolio,
                    daily_pnl=daily_pnl,
                    weekly_pnl=weekly_pnl,
                    latest_candle_close_ts=latest_close,
                    total_open_risk=total_open_risk,
                    symbol_existing_notional=symbol_notional,
                )

                decision = evaluate_risk(
                    conn,
                    settings,
                    signal_id=signal_id,
                    signal_kind=kind,
                    symbol=symbol,
                    proposed_entry_price=proposed_entry_price,
                    proposed_stop_price=proposed_stop_price,
                    strategy_version_id=version_id,
                    strategy_risk_pct=risk_pct,
                    strategy_enabled=enabled,
                    strategy_approved_for_paper=approved_for_paper,
                    inputs=inputs,
                    now=actual_now,
                )

                if decision.kind == "approved":
                    # BUY: use the sized amount from the decision.
                    # EXIT: size=None; the executor reads the open
                    # position's size in Step 7. v1 stores the
                    # open position's size on the order row so the
                    # executor doesn't need a second lookup.
                    if decision.size is not None:
                        order_size = decision.size
                    else:
                        # EXIT: copy the open position's size onto
                        # the order. If there's no open position,
                        # we shouldn't have reached approved — but
                        # defensively skip with a stat.
                        size_from_position = _open_position_size(
                            conn,
                            version_id,
                            symbol,
                        )
                        if size_from_position is None:
                            state.signals_blocked += 1
                            _emit_risk_event(
                                conn,
                                RiskEventType.BLOCK,
                                Severity.WARNING,
                                strategy_version_id=version_id,
                                symbol=symbol,
                                signal_id=signal_id,
                                details={"reason": "exit_without_open_position"},
                            )
                            _mark_signal_processed(conn, signal_id)
                            continue
                        order_size = size_from_position

                    # intended_fill_ts = the OPEN of candle N+1, which
                    # equals the CLOSE of candle N (the signal's
                    # candle_close_ts). The executor in Step 7 fills
                    # at this open. See engine.py for the convention
                    # that anchors backtest parity.
                    _create_paper_order(
                        conn,
                        signal_id=signal_id,
                        strategy_version_id=version_id,
                        symbol=symbol,
                        side="BUY" if kind is SignalKind.BUY else "SELL",
                        size=order_size,
                        intended_fill_ts=candle_close_ts,
                    )
                    state.signals_approved += 1
                else:
                    state.signals_blocked += 1

                _mark_signal_processed(conn, signal_id)

    result = RiskCycleResult(
        signals_processed=state.signals_processed,
        signals_approved=state.signals_approved,
        signals_blocked=state.signals_blocked,
        signals_skipped_no_data=state.signals_skipped_no_data,
        signals_skipped_missing_version=state.signals_skipped_missing_version,
    )
    log.info("risk_cycle_complete", **result.model_dump())
    return result


def _open_position_size(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
) -> Decimal | None:
    """Return the open position's size for the pair, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT size FROM trader_paper_positions
            WHERE strategy_version_id = %s AND symbol = %s AND status = 'OPEN'
            """,
            (str(version_id), symbol),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


__all__ = [
    "RiskCycleResult",
    "RiskInputs",
    "compute_portfolio_state",
    "compute_symbol_existing_notional",
    "compute_total_open_risk",
    "compute_window_pnl",
    "evaluate_risk",
    "latest_candle_close_ts",
    "process_pending_signals",
]
