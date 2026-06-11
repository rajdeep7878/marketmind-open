"""Daily summary — render a DailySummary to operator-facing text.

Pure: `DailySummary -> str`, no I/O, deterministic (same input → same
output). The JSON file is the source of truth; this is the human view.
All money is GBP, all times UTC.
"""

from __future__ import annotations

from marketmind_workers.observability.models import DailySummary, EquitySummary

_MINUTES_PER_DAY = 1440.0


def _money(value: float | None) -> str:
    return f"£{value:,.2f}" if value is not None else "—"


def _signed_money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else "-"
    return f"{sign}£{abs(value):,.2f}"


def _pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "—"


def _age(seconds: float | None) -> str:
    """Compact age — '5s', '3m', '2.1h'."""
    if seconds is None:
        return "—"
    if seconds < 90.0:
        return f"{seconds:.0f}s"
    if seconds < 5400.0:
        return f"{seconds / 60.0:.0f}m"
    return f"{seconds / 3600.0:.1f}h"


def _render_equity(equity: EquitySummary) -> list[str]:
    change = (
        f"{_signed_money(equity.change_24h_gbp)} ({_pct(equity.change_24h_pct)})"
        if equity.change_24h_gbp is not None
        else "—"
    )
    all_time = _signed_money(equity.all_time_pnl_gbp)
    if equity.all_time_pnl_gbp is not None and equity.all_time_since is not None:
        all_time = f"{all_time} since {equity.all_time_since}"
    return [
        "Equity:",
        f"  Current: {_money(equity.current_gbp)}",
        f"  Change last 24h: {change}",
        f"  Open positions: {equity.open_positions}",
        f"  Closed trades last 24h: {equity.closed_trades_24h}",
        f"  All-time P&L: {all_time}",
    ]


def render_summary(summary: DailySummary) -> str:
    """Render the full report. A DOWN bot gets a prominent banner up top."""
    lines: list[str] = [
        f"=== MarketMind Daily Summary — {summary.date} ===",
        f"Generated: {summary.generated_at.isoformat()}",
    ]
    health = summary.bot_health

    if health.status == "DOWN":
        lines += [
            "",
            "!!! BOT NOT RUNNING — "
            f"heartbeat {_age(health.heartbeat_age_seconds)} stale !!!",
        ]

    cycle_rate = health.cycles_24h / _MINUTES_PER_DAY
    lines += [
        "",
        "Bot health:",
        f"  Status: {health.status}",
        f"  Heartbeat: {_age(health.heartbeat_age_seconds)} ago "
        f"({'fresh' if health.heartbeat_fresh else 'stale'})",
        f"  Cycles last 24h: {health.cycles_24h} (~{cycle_rate:.2f}/min)",
        f"  Signal cycles last 24h: {health.signal_cycles_24h} "
        "(expected ~6 for a 4h timeframe)",
        f"  Errors: {health.errors_24h}",
        "",
    ]
    lines += _render_equity(summary.equity)

    active = sum(1 for s in summary.strategies if s.status != "DISABLED")
    lines += ["", f"Strategies ({active} active, {len(summary.strategies)} total):"]
    if not summary.strategies:
        lines.append("  (none seeded)")
    for s in summary.strategies:
        if s.bars_have is not None and s.bars_needed is not None:
            bars = (
                "full"
                if s.bars_have >= s.bars_needed
                else f"{s.bars_have}/{s.bars_needed}"
            )
        else:
            bars = "—"
        last = (
            f"{_age((s.last_cycle_age_hours or 0.0) * 3600.0)} ago, {s.last_decision}"
            if s.last_decision is not None
            else "no activity yet"
        )
        lines += [
            "",
            f"  {s.name} v{s.version} ({s.template} / {s.timeframe} / {s.symbol})",
            f"    Status: {s.status}",
            f"    Last cycle: {last}",
            f"    Bars history: {bars}",
            f"    State rows: {s.state_rows}",
            f"    Trades last 24h: {s.trades_24h}",
        ]

    lines += [
        "",
        f"Risk events last 24h: {summary.risk_events_24h}",
        f"Drift events last 24h: {summary.drift_events_24h}",
        f"Idempotency guard hits last 24h: {summary.idempotency_guard_hits_24h}",
        f"Disable-and-alert events last 24h: {summary.disable_alert_events_24h}",
        "",
        "Notes:",
    ]
    if summary.notes:
        lines += [f"  - {note}" for note in summary.notes]
    else:
        lines.append("  (none)")

    return "\n".join(lines) + "\n"


__all__ = ["render_summary"]
