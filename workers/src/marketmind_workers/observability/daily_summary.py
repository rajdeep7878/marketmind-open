"""Daily summary — generate, persist, and the CLI entry point.

Generates the snapshot, writes it as JSON (the source of truth) plus
rendered text to ``$DATA_DIR/daily-summaries/``, and exposes a CLI:

    docker compose exec trader_worker \\
        python -m marketmind_workers.observability.daily_summary

With no flags the CLI generates a fresh report against the live DB,
writes both files, and prints the rendered text. ``--latest`` instead
renders the most recent already-written report without touching the DB.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import structlog

from marketmind_workers.observability.models import DailySummary
from marketmind_workers.observability.queries import build_daily_summary
from marketmind_workers.observability.render import render_summary

log = structlog.get_logger(__name__)

_SUBDIR = "daily-summaries"


def output_dir() -> Path:
    """``$DATA_DIR/daily-summaries`` — a volume-mounted dir so the files
    survive container restarts. Defaults to ``/data`` (the compose mount).
    """
    return Path(os.environ.get("DATA_DIR", "/data")) / _SUBDIR


def _paths(out: Path, date: str) -> tuple[Path, Path]:
    return out / f"daily-summary-{date}.json", out / f"daily-summary-{date}.txt"


def generate_and_write(
    database_url: str,
    now: datetime,
    out: Path | None = None,
) -> tuple[DailySummary, Path, Path]:
    """Build the snapshot for ``now`` and write JSON + text. Returns the
    summary and the two file paths. Re-running for the same day overwrites
    (the report is a snapshot, not an append log).
    """
    target = out if out is not None else output_dir()
    target.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(database_url) as conn:
        summary = build_daily_summary(conn, now)
    json_path, txt_path = _paths(target, summary.date)
    json_path.write_text(
        json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    txt_path.write_text(render_summary(summary), encoding="utf-8")
    log.info(
        "daily_summary_written",
        date=summary.date,
        json_path=str(json_path),
        txt_path=str(txt_path),
    )
    return summary, json_path, txt_path


def read_latest(out: Path | None = None) -> str | None:
    """Render the most recent already-written report, or None if there
    are no report files yet. ISO-dated filenames sort chronologically.
    """
    target = out if out is not None else output_dir()
    if not target.is_dir():
        return None
    files = sorted(target.glob("daily-summary-*.json"))
    if not files:
        return None
    summary = DailySummary.model_validate_json(files[-1].read_text(encoding="utf-8"))
    return render_summary(summary)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="daily_summary",
        description="MarketMind paper-trading bot — daily summary report.",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Render the most recent already-written report instead of "
        "generating a fresh one (no DB access).",
    )
    args = parser.parse_args(argv)

    if args.latest:
        text = read_latest()
        if text is None:
            print("No daily summary reports found yet.", file=sys.stderr)
            return 1
        print(text, end="")
        return 0

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1
    summary, _, _ = generate_and_write(database_url, datetime.now(UTC))
    print(render_summary(summary), end="")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
