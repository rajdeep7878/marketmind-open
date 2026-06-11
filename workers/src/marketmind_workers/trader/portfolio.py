"""Trader v1 portfolio manager.

`compute_and_persist_snapshot(database_url, settings)` is the
public entry point — the Step 12 runner calls it AFTER each
execution cycle (Step 7) to record the trader's state into
`trader_portfolio_snapshots`. The risk manager (Step 6) and the
drift analyzer (Step 9) read from this table; without snapshots,
they fall back to `settings.trader_starting_cash_gbp` (cold-start
behaviour documented in `risk.compute_portfolio_state`).

CASH RECONSTRUCTION (load-bearing — backtest parity)
====================================================
Cash is computed from scratch every snapshot, NOT incrementally
tracked. This works because every paper cash flow lives in
`trader_paper_fills`:

  net_cash_change_per_fill =
      side == BUY  -> -(notional + fee)   # cash leaves wallet
      side == SELL -> +(notional - fee)   # cash enters wallet

  cash = starting_cash + SUM(net_cash_change_per_fill across all fills)

Stop-closes go through the synthetic signal+order+fill chain in
Step 7, so they appear in `trader_paper_fills` like any other
SELL — no special case here.

EQUITY = cash + sum(MTM of OPEN positions)
  MTM = position.size * latest_close_price_for_the_symbol.

UNREALISED PNL on an open position = (latest_close - entry_price) * size.
  Does NOT subtract a hypothetical close fee — that fee only
  materialises when the position actually closes. Matches vbt's
  open-trade PnL convention.

REALISED PNL CUMULATIVE = SUM(realised_pnl) on CLOSED positions.
  Each closed position's realised_pnl is already net of both
  fees (Step 7's convention).

PEAK EQUITY = max(previous snapshot's peak, current equity).
  Falls back to starting_equity if no prior snapshot exists.

DRAWDOWN = peak_equity - equity (zero when at peak).
DRAWDOWN_PCT = drawdown / peak_equity (zero when peak == 0).

BREAKDOWNS: `per_strategy_breakdown` and `per_symbol_breakdown` are
JSONB dicts of `{key: {realised_pnl, unrealised_pnl, open_positions}}`.
Decimal values are stringified for JSONB round-trip safety.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
import structlog
from marketmind_shared.schemas.trader import PortfolioSnapshot
from marketmind_shared.trader.money import to_decimal
from psycopg.types.json import Jsonb

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.heartbeat import touch_heartbeat

log = structlog.get_logger(__name__)


# ---- Internal query helpers ------------------------------------------------


def _net_cash_from_fills(conn: psycopg.Connection[Any]) -> Decimal:
    """Sum of net cash effects across every fill.

    BUY fill drains cash by (notional + fee).
    SELL fill (including EXIT and synthetic stop-close) adds cash
    by (notional - fee). The `JOIN` to orders is what disambiguates
    BUY vs SELL — fills themselves don't carry the side.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(
                CASE o.side
                    WHEN 'BUY'  THEN -(f.notional + f.fee)
                    WHEN 'SELL' THEN  (f.notional - f.fee)
                    ELSE 0
                END
            ), 0)
            FROM trader_paper_fills f
            JOIN trader_paper_orders o ON o.id = f.order_id
            """,
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return Decimal("0")
    return to_decimal(row[0])


@dataclass(frozen=True)
class _OpenPositionRow:
    position_id: UUID
    strategy_version_id: UUID
    symbol: str
    size: Decimal
    entry_price: Decimal


def _load_open_positions(conn: psycopg.Connection[Any]) -> list[_OpenPositionRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, strategy_version_id, symbol, size, entry_price
            FROM trader_paper_positions
            WHERE status = 'OPEN'
            """,
        )
        rows = cur.fetchall()
    return [
        _OpenPositionRow(
            position_id=UUID(str(r[0])),
            strategy_version_id=UUID(str(r[1])),
            symbol=r[2],
            size=r[3],
            entry_price=r[4],
        )
        for r in rows
    ]


@dataclass(frozen=True)
class _ClosedPositionRow:
    strategy_version_id: UUID
    symbol: str
    realised_pnl: Decimal


def _load_closed_positions(conn: psycopg.Connection[Any]) -> list[_ClosedPositionRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT strategy_version_id, symbol, COALESCE(realised_pnl, 0)
            FROM trader_paper_positions
            WHERE status = 'CLOSED'
            """,
        )
        rows = cur.fetchall()
    return [
        _ClosedPositionRow(
            strategy_version_id=UUID(str(r[0])),
            symbol=r[1],
            realised_pnl=to_decimal(r[2]),
        )
        for r in rows
    ]


def _latest_close_per_symbol(
    conn: psycopg.Connection[Any],
    symbols: list[str],
) -> dict[str, Decimal]:
    """Return ``{symbol: latest closed-candle close price}`` for each
    requested symbol. Symbols without any closed candle yet are
    absent from the result — caller falls back to the position's
    entry_price (zero unrealised PnL).
    """
    if not symbols:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (symbol) symbol, close
            FROM trader_candles
            WHERE is_closed = TRUE AND symbol = ANY(%s)
            ORDER BY symbol, close_ts DESC
            """,
            (symbols,),
        )
        rows = cur.fetchall()
    return {row[0]: row[1] for row in rows}


def _previous_peak_equity(
    conn: psycopg.Connection[Any],
    starting_equity: Decimal,
) -> Decimal:
    """Most recent snapshot's `peak_equity`, or `starting_equity`
    if no snapshot exists yet (cold start).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT peak_equity FROM trader_portfolio_snapshots ORDER BY ts DESC LIMIT 1",
        )
        row = cur.fetchone()
    if row is None:
        return starting_equity
    return to_decimal(row[0])


# ---- Pure aggregation helpers (no DB) --------------------------------------


@dataclass
class _BreakdownEntry:
    """One row of `per_strategy_breakdown` or `per_symbol_breakdown`.

    Values stored as Decimal during aggregation; stringified on
    JSONB serialization to dodge float-precision issues that would
    show up if a Decimal slipped through Pydantic's JSON encoder
    as a float.
    """

    realised_pnl: Decimal = Decimal("0")
    unrealised_pnl: Decimal = Decimal("0")
    open_positions: int = 0

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "realised_pnl": str(self.realised_pnl),
            "unrealised_pnl": str(self.unrealised_pnl),
            "open_positions": self.open_positions,
        }


def _aggregate_breakdowns(
    open_positions: list[_OpenPositionRow],
    closed_positions: list[_ClosedPositionRow],
    latest_close: dict[str, Decimal],
) -> tuple[dict[str, _BreakdownEntry], dict[str, _BreakdownEntry]]:
    """Build per-strategy and per-symbol breakdowns.

    Pure function over already-loaded rows. Per-position unrealised
    PnL uses `latest_close[symbol]` if available; otherwise falls
    back to the position's entry_price (zero unrealised). Realised
    PnL on closed positions flows in unchanged.
    """
    per_strategy: dict[str, _BreakdownEntry] = {}
    per_symbol: dict[str, _BreakdownEntry] = {}

    for pos in open_positions:
        mark = latest_close.get(pos.symbol, pos.entry_price)
        unrealised = (mark - pos.entry_price) * pos.size
        key_strategy = str(pos.strategy_version_id)
        per_strategy.setdefault(key_strategy, _BreakdownEntry())
        per_strategy[key_strategy].unrealised_pnl += unrealised
        per_strategy[key_strategy].open_positions += 1
        per_symbol.setdefault(pos.symbol, _BreakdownEntry())
        per_symbol[pos.symbol].unrealised_pnl += unrealised
        per_symbol[pos.symbol].open_positions += 1

    for cp in closed_positions:
        key_strategy = str(cp.strategy_version_id)
        per_strategy.setdefault(key_strategy, _BreakdownEntry())
        per_strategy[key_strategy].realised_pnl += cp.realised_pnl
        per_symbol.setdefault(cp.symbol, _BreakdownEntry())
        per_symbol[cp.symbol].realised_pnl += cp.realised_pnl

    return per_strategy, per_symbol


# ---- Public API ------------------------------------------------------------


def compute_and_persist_snapshot(
    database_url: str,
    settings: TraderSettings,
    *,
    run_id: UUID | None = None,
) -> PortfolioSnapshot:
    """Compute the trader's current state and INSERT one
    `trader_portfolio_snapshots` row.

    Returns the inserted snapshot (with DB-assigned `id` and `ts`).
    The Step 12 runner calls this after each execution cycle so the
    risk manager + drift analyzer have fresh state on the next pass.

    Implementation notes:
      - One read-only transaction for the snapshot computation,
        then a separate write transaction for the INSERT. Allows
        concurrent reads of the snapshot table during computation.
      - All Decimal math; no float intermediate values.
    """
    starting_cash = to_decimal(settings.trader_starting_cash_gbp)

    with psycopg.connect(database_url) as conn:
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="snapshot")
        cash = starting_cash + _net_cash_from_fills(conn)
        open_positions = _load_open_positions(conn)
        closed_positions = _load_closed_positions(conn)
        symbols = sorted({p.symbol for p in open_positions})
        latest_close = _latest_close_per_symbol(conn, symbols)

        mtm_total = Decimal("0")
        unrealised_total = Decimal("0")
        for pos in open_positions:
            mark = latest_close.get(pos.symbol, pos.entry_price)
            mtm_total += pos.size * mark
            unrealised_total += (mark - pos.entry_price) * pos.size

        realised_cumulative = sum(
            (cp.realised_pnl for cp in closed_positions),
            start=Decimal("0"),
        )

        equity = cash + mtm_total

        previous_peak = _previous_peak_equity(conn, starting_equity=starting_cash)
        peak_equity = max(previous_peak, equity)
        drawdown = peak_equity - equity
        # Guard against div-by-zero on a pathological starting_cash=0.
        # peak_equity > 0 holds in every real scenario.
        drawdown_pct = drawdown / peak_equity if peak_equity > Decimal("0") else Decimal("0")

        per_strategy, per_symbol = _aggregate_breakdowns(
            open_positions,
            closed_positions,
            latest_close,
        )
        per_strategy_jsonable = {k: v.to_jsonable() for k, v in per_strategy.items()}
        per_symbol_jsonable = {k: v.to_jsonable() for k, v in per_symbol.items()}

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO trader_portfolio_snapshots
                    (cash, equity, unrealised_pnl, realised_pnl_cumulative,
                     peak_equity, drawdown, drawdown_pct, open_positions_count,
                     per_strategy_breakdown, per_symbol_breakdown)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, ts
                """,
                (
                    cash,
                    equity,
                    unrealised_total,
                    realised_cumulative,
                    peak_equity,
                    drawdown,
                    drawdown_pct,
                    len(open_positions),
                    Jsonb(per_strategy_jsonable),
                    Jsonb(per_symbol_jsonable),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            snapshot_id, snapshot_ts = row
            conn.commit()

    snapshot = PortfolioSnapshot(
        id=snapshot_id,
        ts=snapshot_ts,
        cash=cash,
        equity=equity,
        unrealised_pnl=unrealised_total,
        realised_pnl_cumulative=realised_cumulative,
        peak_equity=peak_equity,
        drawdown=drawdown,
        drawdown_pct=drawdown_pct,
        open_positions_count=len(open_positions),
        per_strategy_breakdown=per_strategy_jsonable,
        per_symbol_breakdown=per_symbol_jsonable,
    )
    log.info(
        "portfolio_snapshot_persisted",
        snapshot_id=snapshot_id,
        cash=str(cash),
        equity=str(equity),
        peak_equity=str(peak_equity),
        drawdown_pct=str(drawdown_pct),
        open_positions=len(open_positions),
    )
    return snapshot


def fetch_latest_snapshot(database_url: str) -> PortfolioSnapshot | None:
    """Read the most recent snapshot, or None if no snapshot exists.

    Used by Step 11's GET /trader/portfolio/current endpoint.
    """
    with (
        psycopg.connect(database_url) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """
            SELECT id, ts, cash, equity, unrealised_pnl, realised_pnl_cumulative,
                   peak_equity, drawdown, drawdown_pct, open_positions_count,
                   per_strategy_breakdown, per_symbol_breakdown
            FROM trader_portfolio_snapshots
            ORDER BY ts DESC LIMIT 1
            """,
        )
        row = cur.fetchone()
    if row is None:
        return None
    return PortfolioSnapshot(
        id=row[0],
        ts=row[1],
        cash=row[2],
        equity=row[3],
        unrealised_pnl=row[4],
        realised_pnl_cumulative=row[5],
        peak_equity=row[6],
        drawdown=row[7],
        drawdown_pct=row[8],
        open_positions_count=row[9],
        per_strategy_breakdown=dict(row[10]) if row[10] else {},
        per_symbol_breakdown=dict(row[11]) if row[11] else {},
    )


def fetch_equity_curve(
    database_url: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[tuple[datetime, Decimal]]:
    """Return `[(ts, equity), ...]` ascending. Used by
    Step 11's GET /trader/portfolio/equity_curve.

    Filters are handled in Python rather than f-string SQL because
    f-string query construction trips pyright's `str` vs `SQL` type
    distinction. The bounds are inclusive on both sides when present;
    over-fetching by a few rows costs nothing here (snapshot table is
    one row per signal-execution cycle ≈ 6/day at 4h).
    """
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ts, equity FROM trader_portfolio_snapshots
            ORDER BY ts ASC
            """,
        )
        rows = cur.fetchall()
    if since is None and until is None:
        return rows
    return [
        (ts, equity)
        for ts, equity in rows
        if (since is None or ts >= since) and (until is None or ts <= until)
    ]


__all__ = [
    "compute_and_persist_snapshot",
    "fetch_equity_curve",
    "fetch_latest_snapshot",
]
