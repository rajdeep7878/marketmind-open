"""FTR report CLI — `python -m marketmind_workers.ftr.report <command>`.

Commands:
    verdicts   — strategy x venue-profile verdict matrix (latest run)
    equity     — paper-trader equity snapshots (requires DB)
    decisions  — recent paper decisions including skips (requires DB)
    cost-edge  — cost vs gross-edge per strategy/profile

Reads JSON artifacts under data/ftr/artifacts/validation/ (always
available) and Postgres when reachable. All numbers are net of the named
cost profile; wording is descriptive only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from marketmind_workers.ftr.config.settings import get_ftr_settings


def _latest_report(base: Path, family: str) -> dict[str, Any] | None:
    fam_dir = base / family
    if not fam_dir.exists():
        return None
    runs = sorted(d for d in fam_dir.iterdir() if d.is_dir())
    if not runs:
        return None
    report = runs[-1] / "report.json"
    if not report.exists():
        return None
    return json.loads(report.read_text())


def _fmt(value: Any, nd: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{nd}f}"
    return str(value)


def cmd_verdicts() -> int:
    settings = get_ftr_settings()
    base = settings.artifacts_dir / "validation"
    header = (
        f"{'strategy':<28} {'profile':<28} {'verdict':<36} "
        f"{'net_ret':>8} {'sharpe':>7} {'tr/day':>7}  failed_gates"
    )
    print(header)
    print("-" * len(header))
    for family in ("ml_hourly", "trend_4h", "ofi_research"):
        rep = _latest_report(base, family)
        if rep is None:
            print(f"{family:<28} (no validation run found)")
            continue
        strategy = rep.get("strategy", family)
        if "verdict_all_profiles" in rep:
            print(
                f"{strategy:<28} {'ALL':<28} {rep['verdict_all_profiles']:<36} "
                f"{'—':>8} {'—':>7} {'—':>7}  "
                + (rep.get("note", "") or "")[:60]
            )
            continue
        for prof, body in rep.get("profiles", {}).items():
            r = body.get("report", {})
            metrics = r.get("metrics", {})
            print(
                f"{strategy:<28} {prof:<28} {r.get('verdict', '?'):<36} "
                f"{_fmt(metrics.get('net_total_return')):>8} "
                f"{_fmt(metrics.get('sharpe'), 2):>7} "
                f"{_fmt(metrics.get('trades_per_day')):>7}  "
                + ",".join(r.get("failed_gates", []))
            )
    return 0


def cmd_cost_edge() -> int:
    settings = get_ftr_settings()
    base = settings.artifacts_dir / "validation"
    for family in ("ml_hourly", "trend_4h"):
        rep = _latest_report(base, family)
        if rep is None:
            continue
        print(f"\n== {rep.get('strategy', family)} ==")
        for prof, body in rep.get("profiles", {}).items():
            r = body.get("report", {})
            metrics = r.get("metrics", {})
            print(
                f"  {prof:<30} gross={_fmt(metrics.get('gross_total_return'))} "
                f"net={_fmt(metrics.get('net_total_return'))} "
                f"cost_paid={_fmt(metrics.get('cost_paid_frac'))} "
                f"cost/edge={_fmt(metrics.get('cost_over_gross_edge'), 2)}"
            )
            ablation = body.get("overlay_ablation")
            if ablation and "net_with_overlay" in ablation:
                print(
                    f"    overlay ablation: without={_fmt(ablation['net_without_overlay'], 5)} "
                    f"with={_fmt(ablation['net_with_overlay'], 5)} "
                    f"(deferred/skipped {ablation.get('entries_deferred_or_skipped', 0)} entries)"
                )
    return 0


def _db_rows(sql: str, limit: int) -> list[tuple[Any, ...]]:
    import psycopg

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set — equity/decisions need the DB")
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(sql, (limit,))  # type: ignore[arg-type]
        return list(cur.fetchall())


def cmd_equity(limit: int) -> int:
    rows = _db_rows(
        "SELECT ts_utc, equity::float8, gross_exposure_pct FROM ftr_equity_snapshots "
        "ORDER BY ts_utc DESC LIMIT %s",
        limit,
    )
    for ts, equity, gross in reversed(rows):
        print(f"{ts}  equity={equity:,.2f}  gross={gross:.1%}")
    return 0


def cmd_decisions(limit: int) -> int:
    rows = _db_rows(
        "SELECT ts_utc, strategy_id, symbol, action, reason_codes FROM ftr_decisions "
        "ORDER BY ts_utc DESC LIMIT %s",
        limit,
    )
    for ts, strategy_id, symbol, action, reasons in rows:
        print(f"{ts}  {strategy_id:<24} {symbol:<10} {action:<10} {','.join(reasons)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verdicts")
    sub.add_parser("cost-edge")
    eq = sub.add_parser("equity")
    eq.add_argument("--limit", type=int, default=50)
    de = sub.add_parser("decisions")
    de.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if args.command == "verdicts":
        return cmd_verdicts()
    if args.command == "cost-edge":
        return cmd_cost_edge()
    if args.command == "equity":
        return cmd_equity(args.limit)
    if args.command == "decisions":
        return cmd_decisions(args.limit)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
