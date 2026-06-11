"""Daily summary — query functions over the trader's tables.

Each function reads one slice of the snapshot and is testable in
isolation. `now` is always passed explicitly (never SQL `NOW()`) so a
fixture DB produces a deterministic report — the snapshot tests depend
on it. All money is GBP; all times UTC. Every query tolerates missing
data — an empty table yields a zeroed/None field, never a crash.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, LiteralString

import psycopg
import structlog
from marketmind_shared.schemas.trader import TemplateName

from marketmind_workers.observability.models import (
    BotHealth,
    DailySummary,
    EquitySummary,
    StrategySummary,
)
from marketmind_workers.trader.templates import build_template

log = structlog.get_logger(__name__)

_DAY = timedelta(hours=24)
# Heartbeat older than this ⇒ the bot is treated as not running.
_HEARTBEAT_FRESH_SECONDS: float = 300.0
# Hours per timeframe — used to estimate a warmup strategy's ETA.
_TIMEFRAME_HOURS: dict[str, float] = {
    "15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0,
}


def _scalar(cur: psycopg.Cursor[Any], sql: LiteralString, params: tuple[Any, ...]) -> int:
    """Run a COUNT-style query, returning an int (0 if NULL/no row)."""
    cur.execute(sql, params)
    row = cur.fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def query_bot_health(conn: psycopg.Connection[Any], now: datetime) -> BotHealth:
    """Heartbeat freshness + trailing-24h cycle activity and error count."""
    cutoff = now - _DAY
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_heartbeat_at FROM trader_bot_runs "
            "WHERE status = 'running' ORDER BY last_heartbeat_at DESC LIMIT 1",
        )
        row = cur.fetchone()
        hb_age: float | None = None
        if row is not None and row[0] is not None:
            hb_age = (now - row[0]).total_seconds()
        hb_fresh = hb_age is not None and 0.0 <= hb_age <= _HEARTBEAT_FRESH_SECONDS

        # A cycle ≈ one minute-bucket with audit activity (the trader ticks
        # ~1/min). signal_cycles ≈ distinct 4h candles actually evaluated.
        cycles = _scalar(
            cur,
            "SELECT COUNT(DISTINCT date_trunc('minute', ts)) FROM trader_audit_logs "
            "WHERE ts > %s AND ts <= %s",
            (cutoff, now),
        )
        signal_cycles = _scalar(
            cur,
            "SELECT COUNT(DISTINCT payload->>'candle_close_ts') FROM trader_audit_logs "
            "WHERE event = 'hold_decision' AND ts > %s AND ts <= %s",
            (cutoff, now),
        )
        errors = _scalar(
            cur,
            "SELECT COUNT(*) FROM trader_alerts "
            "WHERE severity IN ('error', 'critical') AND ts > %s AND ts <= %s",
            (cutoff, now),
        )

    if hb_age is None or hb_age > _HEARTBEAT_FRESH_SECONDS:
        status: str = "DOWN"
    elif errors > 0:
        status = "DEGRADED"
    else:
        status = "HEALTHY"
    return BotHealth(
        status=status,  # type: ignore[arg-type]
        heartbeat_age_seconds=hb_age,
        heartbeat_fresh=hb_fresh,
        cycles_24h=cycles,
        signal_cycles_24h=signal_cycles,
        errors_24h=errors,
    )


def query_equity(conn: psycopg.Connection[Any], now: datetime) -> EquitySummary:
    """Current equity, the 24h change, open positions, all-time P&L (GBP)."""
    cutoff = now - _DAY
    with conn.cursor() as cur:
        cur.execute(
            "SELECT equity, realised_pnl_cumulative, open_positions_count "
            "FROM trader_portfolio_snapshots WHERE ts <= %s ORDER BY ts DESC LIMIT 1",
            (now,),
        )
        latest = cur.fetchone()
        cur.execute(
            "SELECT equity FROM trader_portfolio_snapshots WHERE ts <= %s "
            "ORDER BY ts DESC LIMIT 1",
            (cutoff,),
        )
        prior = cur.fetchone()
        cur.execute("SELECT MIN(ts) FROM trader_portfolio_snapshots")
        earliest = cur.fetchone()
        closed = _scalar(
            cur,
            "SELECT COUNT(*) FROM trader_paper_positions WHERE status = 'closed' "
            "AND exit_ts > %s AND exit_ts <= %s",
            (cutoff, now),
        )

    if latest is None:
        return EquitySummary(closed_trades_24h=closed)
    current = float(latest[0]) if latest[0] is not None else None
    all_time_pnl = float(latest[1]) if latest[1] is not None else None
    open_positions = int(latest[2]) if latest[2] is not None else 0
    change_gbp: float | None = None
    change_pct: float | None = None
    if current is not None and prior is not None and prior[0] is not None:
        prior_equity = float(prior[0])
        change_gbp = current - prior_equity
        change_pct = (change_gbp / prior_equity * 100.0) if prior_equity else None
    since: str | None = None
    if earliest is not None and earliest[0] is not None:
        since = earliest[0].date().isoformat()
    return EquitySummary(
        current_gbp=current,
        change_24h_gbp=change_gbp,
        change_24h_pct=change_pct,
        open_positions=open_positions,
        closed_trades_24h=closed,
        all_time_pnl_gbp=all_time_pnl,
        all_time_since=since,
    )


def _min_bars_needed(template: str, parameters: dict[str, Any]) -> int | None:
    """`min_bars_needed()` for a version's template, or None if it can't
    be built (a malformed spec — tolerated, not fatal to the report).
    """
    try:
        return build_template(TemplateName(template), parameters).min_bars_needed()
    except Exception:  # a malformed spec must not break the whole summary
        log.warning("daily_summary_min_bars_failed", template=template)
        return None


def _last_decision(
    last_audit: tuple[Any, ...] | None,
    last_signal: tuple[Any, ...] | None,
    now: datetime,
) -> tuple[str | None, float | None]:
    """The most recent decision (a fired signal or a HOLD) and its age in
    hours — whichever of the audit-log HOLD or the trader_signals row is
    newer.
    """
    candidates: list[tuple[datetime, str]] = []
    if last_audit is not None and last_audit[1] is not None:
        candidates.append((last_audit[1], "HOLD"))
    if last_signal is not None and last_signal[1] is not None:
        candidates.append((last_signal[1], str(last_signal[0]).upper()))
    if not candidates:
        return None, None
    ts, decision = max(candidates, key=lambda c: c[0])
    return decision, (now - ts).total_seconds() / 3600.0


def query_strategies(conn: psycopg.Connection[Any], now: datetime) -> list[StrategySummary]:
    """Every strategy version, earliest-seeded first, with its current
    status and trailing-24h activity.
    """
    cutoff = now - _DAY
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.id, s.name, v.version, v.template, v.symbols, v.timeframes, "
            "v.enabled, v.parameters "
            "FROM trader_strategy_versions v "
            "JOIN trader_strategies s ON s.id = v.strategy_id "
            "ORDER BY v.created_at ASC, v.id ASC",
        )
        versions = cur.fetchall()

    out: list[StrategySummary] = []
    for vid, name, version, template, symbols, timeframes, enabled, parameters in versions:
        symbol = symbols[0] if symbols else "—"
        timeframe = timeframes[0] if timeframes else "—"
        with conn.cursor() as cur:
            state_rows = _scalar(
                cur,
                "SELECT COUNT(*) FROM trader_strategy_state WHERE strategy_version_id = %s",
                (str(vid),),
            )
            trades = _scalar(
                cur,
                "SELECT COUNT(*) FROM trader_paper_positions WHERE strategy_version_id = %s "
                "AND status = 'closed' AND exit_ts > %s AND exit_ts <= %s",
                (str(vid), cutoff, now),
            )
            open_positions = _scalar(
                cur,
                "SELECT COUNT(*) FROM trader_paper_positions "
                "WHERE strategy_version_id = %s AND status = 'open'",
                (str(vid),),
            )
            bars_have = _scalar(
                cur,
                "SELECT COUNT(*) FROM trader_candles "
                "WHERE symbol = %s AND timeframe = %s AND is_closed",
                (symbol, timeframe),
            )
            cur.execute(
                "SELECT event, ts FROM trader_audit_logs "
                "WHERE entity_type = 'trader_strategy_versions' AND entity_id = %s "
                "AND ts <= %s ORDER BY ts DESC LIMIT 1",
                (str(vid), now),
            )
            last_audit = cur.fetchone()
            cur.execute(
                "SELECT signal, created_at FROM trader_signals "
                "WHERE strategy_version_id = %s AND created_at <= %s "
                "ORDER BY created_at DESC LIMIT 1",
                (str(vid), now),
            )
            last_signal = cur.fetchone()

        bars_needed = _min_bars_needed(template, parameters)
        last_decision, last_age = _last_decision(last_audit, last_signal, now)
        if not enabled:
            status: str = "DISABLED"
        elif open_positions > 0:
            status = "IN_POSITION"
        elif bars_needed is not None and bars_have < bars_needed:
            status = "WARMUP"
        else:
            status = "EVALUATING"
        out.append(
            StrategySummary(
                name=name,
                version=version,
                template=template,
                timeframe=timeframe,
                symbol=symbol,
                status=status,  # type: ignore[arg-type]
                last_decision=last_decision,
                last_cycle_age_hours=last_age,
                bars_have=bars_have,
                bars_needed=bars_needed,
                state_rows=state_rows,
                trades_24h=trades,
            ),
        )
    return out


def query_event_counts(
    conn: psycopg.Connection[Any], now: datetime,
) -> tuple[int, int, int]:
    """Trailing-24h counts: (risk_events, drift_events, disable_alert_events)."""
    cutoff = now - _DAY
    with conn.cursor() as cur:
        risk = _scalar(
            cur,
            "SELECT COUNT(*) FROM trader_risk_events WHERE ts > %s AND ts <= %s",
            (cutoff, now),
        )
        drift = _scalar(
            cur,
            "SELECT COUNT(*) FROM trader_drift_metrics WHERE ts > %s AND ts <= %s",
            (cutoff, now),
        )
        disable_alerts = _scalar(
            cur,
            "SELECT COUNT(*) FROM trader_alerts "
            "WHERE subject LIKE '%%auto-disabled%%' AND ts > %s AND ts <= %s",
            (cutoff, now),
        )
    return risk, drift, disable_alerts


def _build_notes(health: BotHealth, strategies: list[StrategySummary]) -> list[str]:
    """Auto-generated callouts — warmup ETAs, disabled strategies, a
    prominent bot-down line.
    """
    notes: list[str] = []
    if health.status == "DOWN":
        age = health.heartbeat_age_seconds
        stale = f"{age / 3600.0:.1f}h" if age is not None else "unknown duration"
        notes.append(f"BOT NOT RUNNING — heartbeat {stale} stale.")
    for s in strategies:
        if s.status == "WARMUP" and s.bars_have is not None and s.bars_needed is not None:
            gap = s.bars_needed - s.bars_have
            tf_hours = _TIMEFRAME_HOURS.get(s.timeframe)
            eta = f"~{gap * tf_hours / 24.0:.1f} days" if tf_hours else f"{gap} bars"
            notes.append(
                f"{s.name} v{s.version} in warmup — {s.bars_have}/{s.bars_needed} bars, "
                f"first evaluation {eta} out.",
            )
        elif s.status == "DISABLED":
            notes.append(f"{s.name} v{s.version} is DISABLED — investigate before re-enabling.")
    return notes


def build_daily_summary(conn: psycopg.Connection[Any], now: datetime) -> DailySummary:
    """Assemble the full snapshot. `now` is the report instant — passed
    through to every query so the result is deterministic for a fixed DB.
    """
    health = query_bot_health(conn, now)
    equity = query_equity(conn, now)
    strategies = query_strategies(conn, now)
    risk, drift, disable_alerts = query_event_counts(conn, now)
    return DailySummary(
        date=now.date().isoformat(),
        generated_at=now,
        bot_health=health,
        equity=equity,
        strategies=strategies,
        risk_events_24h=risk,
        drift_events_24h=drift,
        # idempotency_guard_hits is a per-cycle log stat (signal_cycle_complete
        # `pair_state_guarded`) with no queryable store — left at 0 by design.
        idempotency_guard_hits_24h=0,
        disable_alert_events_24h=disable_alerts,
        notes=_build_notes(health, strategies),
    )


__all__ = [
    "build_daily_summary",
    "query_bot_health",
    "query_equity",
    "query_event_counts",
    "query_strategies",
]
