"""Trader v1 paper execution engine.

FILL POLICY (LOAD-BEARING — BACKTEST PARITY)
============================================
This module's fill semantics MUST match MarketMind's backtest engine
exactly (workers/backtest/engine.py + the Step 3 parity contract on
`atr_stop_for_long`). Divergence here breaks the paper-vs-backtest
drift comparison the trader is anchored to.

1. Signal fires on CLOSE of candle N. Step 5's signal engine writes
   the row with `candle_close_ts = N.close_ts`.

2. The risk manager (Step 6) creates a PENDING `trader_paper_orders`
   row with `intended_fill_ts = signal.candle_close_ts`. By the
   candle-boundary convention, this equals the OPEN of candle N+1
   (next bar opens at the close of the prior bar).

3. Fill happens at the OPEN of the candle whose `open_ts == intended_fill_ts`.
   Critically — `open_ts`, NOT `close_ts`. Using `close_ts` would
   be either a lookahead-bias bug (filling at N's open) or a
   fill-too-late bug (filling at N+2's open).

       BUY     fill_price = candle_open * (1 + slippage_bps / 10000)
       SELL    fill_price = candle_open * (1 - slippage_bps / 10000)
       EXIT    fill_price = candle_open * (1 - slippage_bps / 10000)

4. Fee per fill = `fill_price * size * fee_bps / 10000`, applied to cash.
   Notional = `fill_price * size`. Both quantised at price precision.

5. STOP-LOSS CHECK runs BEFORE pending-order fills each cycle.
   For every OPEN position, scan candles `WHERE open_ts >= entry_ts
   AND low <= stop_price AND is_closed = TRUE`. The earliest match
   is the breach. Force-close at:

       fill_price = stop_price * (1 - slippage_bps / 10000)
       close_reason = 'stop_hit'

   Same-bar stops ARE possible (entry candle's low can <= stop).

6. NO PARTIAL FILLS in v1. One order ⇒ one fill row (or one
   REJECTED status if the order can't fill: position already closed,
   etc.). The DB enforces this via UNIQUE constraints on
   `trader_paper_orders.signal_id` and `trader_paper_fills.order_id`.

7. STOP-CLOSE AUDIT TRAIL: every stop-hit close generates four rows
   for accounting consistency — a synthetic EXIT signal, a synthetic
   FILLED order, a fill row, and the position update. The synthetic
   signal makes Step 8's cash reconstruction trivial (sum all fills);
   the alternative (position-update-only) would create an asymmetric
   data model.

The pure `_compute_fill(side, reference_price, size, slippage_bps,
fee_bps)` helper is the single source of truth for the fill-price /
fee / notional math. Unit-tested in isolation; every persistence
path delegates to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg
import structlog
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.trader import OrderSide
from marketmind_shared.trader.money import (
    apply_slippage_buy,
    apply_slippage_sell,
    fee_for_fill,
    quantize_price,
)
from marketmind_shared.trader.time import now_utc
from psycopg.types.json import Jsonb

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.heartbeat import touch_heartbeat

log = structlog.get_logger(__name__)


# ---- Pure fill math --------------------------------------------------------


@dataclass(frozen=True)
class _FillCalculation:
    """Result of one fill computation. All values quantised at v1
    price precision. The orchestrator persists these to
    `trader_paper_fills` + uses them in `realised_pnl` math.
    """

    fill_price: Decimal
    fee: Decimal
    notional: Decimal  # fill_price * size
    slippage_bps_applied: Decimal


def _compute_fill(
    *,
    side: OrderSide,
    reference_price: Decimal,
    size: Decimal,
    slippage_bps: Decimal,
    fee_bps: Decimal,
) -> _FillCalculation:
    """Compute fill_price / fee / notional for ONE paper fill.

    `reference_price` is either:
      - The candle's OPEN (for normal entry / exit fills at
        candle N+1's open), or
      - The position's STOP_PRICE (for stop-hit force-closes).

    For BUY: fill above reference by slippage.
    For SELL / EXIT (long-only spot): fill below reference by
    slippage — same formula applies regardless of whether the
    SELL is a signal-driven exit or a stop-hit close.

    Pure function — no DB, no clock, no I/O. The unit tests in
    `test_trader_execution.py::Test_compute_fill` parametrise over
    fee / slippage / size combinations.
    """
    if side is OrderSide.BUY:
        fill_price = apply_slippage_buy(reference_price, slippage_bps)
    else:
        # OrderSide.SELL: signal-driven exit AND stop-hit close
        # both flow through the same arithmetic. v1 is long-only
        # spot, so this is always "we're selling our long".
        fill_price = apply_slippage_sell(reference_price, slippage_bps)
    fee = fee_for_fill(fill_price, size, fee_bps)
    notional = quantize_price(fill_price * size)
    return _FillCalculation(
        fill_price=fill_price,
        fee=fee,
        notional=notional,
        slippage_bps_applied=slippage_bps,
    )


# ---- Result types ----------------------------------------------------------


class ExecutionResult(_StrictModel):
    """Aggregate stats from one execution cycle."""

    open_positions_scanned: int = 0
    positions_closed_by_stop: int = 0
    pending_orders_loaded: int = 0
    pending_orders_filled: int = 0
    pending_orders_waiting: int = 0  # intended_fill_ts candle not yet ingested
    pending_orders_rejected: int = 0  # position already closed / other invariant
    positions_opened: int = 0
    positions_closed_by_signal: int = 0


@dataclass
class _CycleState:
    open_positions_scanned: int = 0
    positions_closed_by_stop: int = 0
    pending_orders_loaded: int = 0
    pending_orders_filled: int = 0
    pending_orders_waiting: int = 0
    pending_orders_rejected: int = 0
    positions_opened: int = 0
    positions_closed_by_signal: int = 0


@dataclass(frozen=True)
class _OpenPositionWithMeta:
    """Slim view of an OPEN trader_paper_positions row joined with
    the version's fee/slippage bps + the entry signal's timeframe.
    Internal — composed from a single SQL JOIN in
    `_load_open_positions_with_meta`.
    """

    position_id: UUID
    strategy_version_id: UUID
    symbol: str
    timeframe: str
    entry_order_id: UUID
    entry_price: Decimal
    entry_ts: datetime
    size: Decimal
    stop_price: Decimal
    take_profit_price: Decimal | None
    fee_bps: Decimal
    slippage_bps: Decimal


@dataclass(frozen=True)
class _PendingOrderWithMeta:
    """Slim view of a PENDING trader_paper_orders row joined with
    the version's fee/slippage bps + the signal's timeframe and
    kind. Internal — composed from a single SQL JOIN in
    `_load_pending_orders_with_meta`.
    """

    order_id: UUID
    signal_id: UUID
    strategy_version_id: UUID
    symbol: str
    timeframe: str
    side: str
    requested_size: Decimal
    intended_fill_ts: datetime
    signal_kind: str
    proposed_stop_price: Decimal
    proposed_take_profit_price: Decimal | None
    fee_bps: Decimal
    slippage_bps: Decimal


# ---- DB-touching helpers ---------------------------------------------------


def _load_open_positions_with_meta(
    conn: psycopg.Connection[Any],
) -> list[_OpenPositionWithMeta]:
    """Load every OPEN paper position joined with the version's
    fee/slippage bps and the entry signal's timeframe. The JOIN
    chain: position → entry_order → entry_signal → version.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id, p.strategy_version_id, p.symbol, s.timeframe,
                p.entry_order_id, p.entry_price, p.entry_ts, p.size,
                p.stop_price, p.take_profit_price,
                v.fee_bps, v.slippage_bps
            FROM trader_paper_positions p
            JOIN trader_paper_orders o ON o.id = p.entry_order_id
            JOIN trader_signals s ON s.id = o.signal_id
            JOIN trader_strategy_versions v ON v.id = p.strategy_version_id
            WHERE p.status = 'OPEN'
            ORDER BY p.entry_ts
            """,
        )
        rows = cur.fetchall()
    return [
        _OpenPositionWithMeta(
            position_id=UUID(str(r[0])),
            strategy_version_id=UUID(str(r[1])),
            symbol=r[2],
            timeframe=r[3],
            entry_order_id=UUID(str(r[4])),
            entry_price=r[5],
            entry_ts=r[6],
            size=r[7],
            stop_price=r[8],
            take_profit_price=r[9],
            fee_bps=r[10],
            slippage_bps=r[11],
        )
        for r in rows
    ]


def _find_first_stop_breach(
    conn: psycopg.Connection[Any],
    pos: _OpenPositionWithMeta,
) -> tuple[datetime, datetime] | None:
    """Find the earliest closed candle since `pos.entry_ts` (inclusive)
    where `low <= pos.stop_price`. Returns `(open_ts, close_ts)`
    of that candle, or None if no breach is in the DB yet.

    Same-bar stops: the entry candle itself counts — a stop in the
    entry bar's range is a valid breach. Match the convention used
    by vbt's `sl_stop`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_ts, close_ts FROM trader_candles
            WHERE symbol = %s AND timeframe = %s AND is_closed = TRUE
              AND open_ts >= %s AND low <= %s
            ORDER BY open_ts ASC
            LIMIT 1
            """,
            (pos.symbol, pos.timeframe, pos.entry_ts, pos.stop_price),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _entry_fee_of(conn: psycopg.Connection[Any], entry_order_id: UUID) -> Decimal:
    """Look up the entry order's fill fee — needed for realised_pnl.

    Both signal-driven entries and stop-closes need this number to
    compute the correct net PnL (entry_fee was paid in cash when
    the position opened; subtracting it here makes realised_pnl
    equal to the total cash effect of the trade end-to-end).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fee FROM trader_paper_fills WHERE order_id = %s",
            (str(entry_order_id),),
        )
        row = cur.fetchone()
    if row is None:
        # Defensive: every OPEN position should have a fill row for
        # its entry_order_id (the executor creates them in lock-step).
        # If absent, assume zero fee — over-reports realised_pnl
        # slightly but doesn't crash the loop.
        return Decimal("0")
    return row[0]


def _execute_stop_close(
    conn: psycopg.Connection[Any],
    pos: _OpenPositionWithMeta,
    breach_open_ts: datetime,
    breach_close_ts: datetime,
) -> None:
    """Phase 1: synthesize signal + order + fill, then close the
    position.

    Why the synthetic chain (load-bearing — must survive refactors):
    the synthetic signal+order+fill rows make `trader_paper_fills`
    the canonical, exception-free source of cash flows for the
    entire trader. Step 8 (portfolio cash reconstruction) and
    Step 9 (drift analyzer trade counts) both sum fill rows; a
    stop-close-without-fill path would force them to special-case
    "look at position table for closes-without-fill". Symmetry now
    is cheaper than asymmetric reconstruction later.

    `exit_ts` = breach candle's `open_ts`. Matches vbt's convention
    (exit timestamp = bar's index = open_ts).
    """
    fill = _compute_fill(
        side=OrderSide.SELL,
        reference_price=pos.stop_price,
        size=pos.size,
        slippage_bps=pos.slippage_bps,
        fee_bps=pos.fee_bps,
    )
    entry_fee = _entry_fee_of(conn, pos.entry_order_id)
    # ---- realised_pnl convention: NET of BOTH fees -----------------
    # Matches MarketMind's backtest engine (vbt.Portfolio.trades.PnL),
    # empirically verified at Step 7 verification:
    #   vbt PnL    = (exit_price - entry_price) * size - entry_fee - exit_fee
    #   vbt Return = vbt PnL / (entry_price * size)
    # Drift parity REQUIRES this convention to match — a fee-handling
    # divergence here would flag every healthy strategy as decaying
    # (~0.2% per trade × 30 trades ≈ 6% spurious gap). Don't change
    # this formula without re-confirming vbt's convention with a
    # fresh empirical test.
    realised_pnl = (fill.fill_price - pos.entry_price) * pos.size - entry_fee - fill.fee
    realised_pnl_pct = realised_pnl / (pos.entry_price * pos.size)

    synthetic_signal_id = uuid4()
    synthetic_order_id = uuid4()
    synthetic_fill_id = uuid4()

    with conn.cursor() as cur:
        # Synthetic signal — the audit trail for "stop hit on this
        # candle". `processed_at` set immediately because the
        # execution is happening right now.
        cur.execute(
            """
            INSERT INTO trader_signals
                (id, strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators,
                 proposed_entry_price, proposed_stop_price, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                str(synthetic_signal_id),
                str(pos.strategy_version_id),
                pos.symbol,
                pos.timeframe,
                breach_close_ts,
                "EXIT",
                "stop_hit",
                Jsonb({"synthetic": True, "stop_price": str(pos.stop_price)}),
                pos.stop_price,
                pos.stop_price,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_orders
                (id, signal_id, strategy_version_id, symbol, side,
                 order_type, requested_size, status, intended_fill_ts)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(synthetic_order_id),
                str(synthetic_signal_id),
                str(pos.strategy_version_id),
                pos.symbol,
                "SELL",
                "MARKET",
                pos.size,
                "FILLED",
                breach_open_ts,
            ),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size,
                 fee, slippage_bps_applied, notional)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(synthetic_fill_id),
                str(synthetic_order_id),
                breach_open_ts,
                fill.fill_price,
                pos.size,
                fill.fee,
                fill.slippage_bps_applied,
                fill.notional,
            ),
        )
        cur.execute(
            """
            UPDATE trader_paper_positions
            SET status = 'CLOSED',
                exit_order_id = %s,
                exit_price = %s,
                exit_ts = %s,
                realised_pnl = %s,
                realised_pnl_pct = %s,
                close_reason = 'stop_hit'
            WHERE id = %s
            """,
            (
                str(synthetic_order_id),
                fill.fill_price,
                breach_open_ts,
                realised_pnl,
                realised_pnl_pct,
                str(pos.position_id),
            ),
        )


def _load_pending_orders_with_meta(
    conn: psycopg.Connection[Any],
) -> list[_PendingOrderWithMeta]:
    """Load every PENDING order joined with version bps + the
    signal's timeframe + kind. Ordered by `intended_fill_ts` so a
    backlog of pending orders fills in chronological order.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                o.id, o.signal_id, o.strategy_version_id, o.symbol,
                s.timeframe, o.side, o.requested_size, o.intended_fill_ts,
                s.signal, s.proposed_stop_price, s.proposed_take_profit_price,
                v.fee_bps, v.slippage_bps
            FROM trader_paper_orders o
            JOIN trader_signals s ON s.id = o.signal_id
            JOIN trader_strategy_versions v ON v.id = o.strategy_version_id
            WHERE o.status = 'PENDING'
            ORDER BY o.intended_fill_ts, o.requested_at
            """,
        )
        rows = cur.fetchall()
    return [
        _PendingOrderWithMeta(
            order_id=UUID(str(r[0])),
            signal_id=UUID(str(r[1])),
            strategy_version_id=UUID(str(r[2])),
            symbol=r[3],
            timeframe=r[4],
            side=r[5],
            requested_size=r[6],
            intended_fill_ts=r[7],
            signal_kind=r[8],
            proposed_stop_price=r[9],
            proposed_take_profit_price=r[10],
            fee_bps=r[11],
            slippage_bps=r[12],
        )
        for r in rows
    ]


def _candle_open_at(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    open_ts: datetime,
) -> Decimal | None:
    """Return the candle's open price at exact `open_ts`, or None.

    Critical: queries on `open_ts == intended_fill_ts`, NOT
    `close_ts`. Using close_ts would either lookahead-bias (the
    PRIOR bar's open) or fill too late (a bar +1 away).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open FROM trader_candles
            WHERE symbol = %s AND timeframe = %s
              AND open_ts = %s AND is_closed = TRUE
            """,
            (symbol, timeframe, open_ts),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def _open_position_id_for(
    conn: psycopg.Connection[Any],
    strategy_version_id: UUID,
    symbol: str,
) -> _OpenPositionWithMeta | None:
    """Look up the open position for an EXIT-order fill. None if
    none exists — caller treats that as a stop-already-closed race
    and rejects the order.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                p.id, p.strategy_version_id, p.symbol, s.timeframe,
                p.entry_order_id, p.entry_price, p.entry_ts, p.size,
                p.stop_price, p.take_profit_price,
                v.fee_bps, v.slippage_bps
            FROM trader_paper_positions p
            JOIN trader_paper_orders o ON o.id = p.entry_order_id
            JOIN trader_signals s ON s.id = o.signal_id
            JOIN trader_strategy_versions v ON v.id = p.strategy_version_id
            WHERE p.strategy_version_id = %s
              AND p.symbol = %s
              AND p.status = 'OPEN'
            """,
            (str(strategy_version_id), symbol),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _OpenPositionWithMeta(
        position_id=UUID(str(row[0])),
        strategy_version_id=UUID(str(row[1])),
        symbol=row[2],
        timeframe=row[3],
        entry_order_id=UUID(str(row[4])),
        entry_price=row[5],
        entry_ts=row[6],
        size=row[7],
        stop_price=row[8],
        take_profit_price=row[9],
        fee_bps=row[10],
        slippage_bps=row[11],
    )


def _execute_buy_fill(
    conn: psycopg.Connection[Any],
    order: _PendingOrderWithMeta,
    candle_open: Decimal,
) -> None:
    """Phase 2: BUY order fill. Creates fill row + opens position.

    The DB's partial unique index `(strategy_version_id, symbol)
    WHERE status = 'OPEN'` is the second line of defence: if
    somehow two BUY orders for the same pair both reach fill at
    once, the second INSERT raises and rolls back, preventing a
    double-open.
    """
    fill = _compute_fill(
        side=OrderSide.BUY,
        reference_price=candle_open,
        size=order.requested_size,
        slippage_bps=order.slippage_bps,
        fee_bps=order.fee_bps,
    )

    fill_id = uuid4()
    position_id = uuid4()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size,
                 fee, slippage_bps_applied, notional)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(fill_id),
                str(order.order_id),
                order.intended_fill_ts,
                fill.fill_price,
                order.requested_size,
                fill.fee,
                fill.slippage_bps_applied,
                fill.notional,
            ),
        )
        cur.execute(
            "UPDATE trader_paper_orders SET status = 'FILLED' WHERE id = %s",
            (str(order.order_id),),
        )
        cur.execute(
            """
            INSERT INTO trader_paper_positions
                (id, strategy_version_id, symbol, side, entry_order_id,
                 entry_price, entry_ts, size, stop_price,
                 take_profit_price, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN')
            """,
            (
                str(position_id),
                str(order.strategy_version_id),
                order.symbol,
                "LONG",
                str(order.order_id),
                fill.fill_price,
                order.intended_fill_ts,
                order.requested_size,
                order.proposed_stop_price,
                order.proposed_take_profit_price,
            ),
        )


def _execute_exit_fill(
    conn: psycopg.Connection[Any],
    order: _PendingOrderWithMeta,
    pos: _OpenPositionWithMeta,
    candle_open: Decimal,
) -> None:
    """Phase 2: SELL/EXIT order fill closing an existing OPEN position.

    `realised_pnl` includes both fees (entry + exit) for the full
    cash effect of the trade.
    """
    fill = _compute_fill(
        side=OrderSide.SELL,
        reference_price=candle_open,
        size=pos.size,
        slippage_bps=order.slippage_bps,
        fee_bps=order.fee_bps,
    )
    entry_fee = _entry_fee_of(conn, pos.entry_order_id)
    # realised_pnl convention: NET of both fees. See `_execute_stop_close`
    # for the full rationale + backtest-parity invariant.
    realised_pnl = (fill.fill_price - pos.entry_price) * pos.size - entry_fee - fill.fee
    realised_pnl_pct = realised_pnl / (pos.entry_price * pos.size)

    fill_id = uuid4()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_paper_fills
                (id, order_id, fill_ts, fill_price, size,
                 fee, slippage_bps_applied, notional)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                str(fill_id),
                str(order.order_id),
                order.intended_fill_ts,
                fill.fill_price,
                pos.size,
                fill.fee,
                fill.slippage_bps_applied,
                fill.notional,
            ),
        )
        cur.execute(
            "UPDATE trader_paper_orders SET status = 'FILLED' WHERE id = %s",
            (str(order.order_id),),
        )
        cur.execute(
            """
            UPDATE trader_paper_positions
            SET status = 'CLOSED',
                exit_order_id = %s,
                exit_price = %s,
                exit_ts = %s,
                realised_pnl = %s,
                realised_pnl_pct = %s,
                close_reason = 'signal_exit'
            WHERE id = %s
            """,
            (
                str(order.order_id),
                fill.fill_price,
                order.intended_fill_ts,
                realised_pnl,
                realised_pnl_pct,
                str(pos.position_id),
            ),
        )


def _reject_order(
    conn: psycopg.Connection[Any],
    order_id: UUID,
    reason: str,
) -> None:
    """Flip a PENDING order to REJECTED with a stored reason."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trader_paper_orders
            SET status = 'REJECTED', rejection_reason = %s
            WHERE id = %s
            """,
            (reason, str(order_id)),
        )


# ---- Orchestrator ----------------------------------------------------------


def process_one_cycle(
    database_url: str,
    settings: TraderSettings,  # reserved for future per-cycle config
    *,
    now: datetime | None = None,  # reserved for future stale-data guards
    run_id: UUID | None = None,
) -> ExecutionResult:
    """Run one paper-execution pass.

    Phase 1: stop-loss scan on every OPEN position. First breach
    per position wins; force-close at `stop_price * (1 - slippage)`
    with `close_reason = 'stop_hit'`. Runs BEFORE Phase 2 so a
    stop-vs-signal-exit race goes to the stop.

    Phase 2: fill every PENDING order whose `intended_fill_ts`
    matches an ingested candle's `open_ts`. BUY → opens a position;
    SELL/EXIT → closes the open position (rejects if no open
    position exists — e.g., the stop in Phase 1 just closed it).

    Each operation runs inside its own `conn.transaction()` block
    so a failure on one position/order doesn't roll back another's
    work. Phases run sequentially within the cycle; concurrent
    workers serialise via the DB's partial-unique-on-OPEN index.
    """
    state = _CycleState()

    with psycopg.connect(database_url) as conn:
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="execute")
        # ---- Phase 1: stop-loss scan ----
        open_positions = _load_open_positions_with_meta(conn)
        state.open_positions_scanned = len(open_positions)
        for pos in open_positions:
            with conn.transaction():
                breach = _find_first_stop_breach(conn, pos)
                if breach is None:
                    continue
                breach_open_ts, breach_close_ts = breach
                _execute_stop_close(conn, pos, breach_open_ts, breach_close_ts)
                state.positions_closed_by_stop += 1
                log.info(
                    "stop_hit",
                    position_id=str(pos.position_id),
                    symbol=pos.symbol,
                    stop_price=str(pos.stop_price),
                    breach_open_ts=breach_open_ts.isoformat(),
                )

        # ---- Phase 2: pending-order fills ----
        pending = _load_pending_orders_with_meta(conn)
        state.pending_orders_loaded = len(pending)

        for order in pending:
            with conn.transaction():
                # Look up the fill candle by its OPEN ts. Critical:
                # NOT close_ts — that would be a lookahead or
                # fill-too-late bug. See module docstring.
                candle_open = _candle_open_at(
                    conn,
                    order.symbol,
                    order.timeframe,
                    order.intended_fill_ts,
                )
                if candle_open is None:
                    state.pending_orders_waiting += 1
                    continue

                if order.side == "BUY":
                    # Defensive: another worker may have opened the
                    # position via a different signal already. The
                    # partial unique index on positions will raise on
                    # INSERT — let it; the transaction rolls back
                    # and the order remains PENDING for next cycle.
                    _execute_buy_fill(conn, order, candle_open)
                    state.pending_orders_filled += 1
                    state.positions_opened += 1
                else:
                    # SELL / EXIT: find the open position. If the
                    # stop in Phase 1 already closed it, there's
                    # nothing to exit — reject the order.
                    pos = _open_position_id_for(
                        conn,
                        order.strategy_version_id,
                        order.symbol,
                    )
                    if pos is None:
                        _reject_order(
                            conn,
                            order.order_id,
                            "position_already_closed",
                        )
                        state.pending_orders_rejected += 1
                        log.info(
                            "exit_rejected_no_open_position",
                            order_id=str(order.order_id),
                            symbol=order.symbol,
                        )
                        continue
                    _execute_exit_fill(conn, order, pos, candle_open)
                    state.pending_orders_filled += 1
                    state.positions_closed_by_signal += 1

    result = ExecutionResult(
        open_positions_scanned=state.open_positions_scanned,
        positions_closed_by_stop=state.positions_closed_by_stop,
        pending_orders_loaded=state.pending_orders_loaded,
        pending_orders_filled=state.pending_orders_filled,
        pending_orders_waiting=state.pending_orders_waiting,
        pending_orders_rejected=state.pending_orders_rejected,
        positions_opened=state.positions_opened,
        positions_closed_by_signal=state.positions_closed_by_signal,
    )
    log.info("execution_cycle_complete", **result.model_dump())
    return result


# `now` import retained for future use (stale-candle guard);
# silence the unused-import warning by referencing it here.
_ = now_utc

__all__ = ["ExecutionResult", "process_one_cycle"]
