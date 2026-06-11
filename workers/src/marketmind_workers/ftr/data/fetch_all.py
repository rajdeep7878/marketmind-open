"""Stage-1 data pull: full 1h/4h history + trailing-180d 1m bars.

Usage:
    python -m marketmind_workers.ftr.data.fetch_all [--skip-1m] [--days-1m N]

- 1h and 4h: full available history for the universe superset (Binance,
  reference research series) — Binance spot listing is verified against a
  uk_execution_feasible venue (Kraken) and any symbol not listed there is
  dropped from the trend universe (logged in the manifest).
- 1m: trailing N days (default 180) for paper-fill modeling and overlay
  spread estimation. Storage cost is recorded in the fetch manifest
  (~8-12 MB/symbol snappy parquet for 180d).
- BTC/USD from Kraken as the cross-venue sanity series (1h).
- Data QA runs on every series; reports are written to
  ``data/ftr/artifacts/qa/`` as JSON (and to ftr_data_quality when the DB
  is reachable — the fetch CLI works without a DB).
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog

from marketmind_workers.ftr.config.settings import UNIVERSE_SUPERSET, get_ftr_settings
from marketmind_workers.ftr.data.ohlcv import get_ohlcv, verify_spot_listing
from marketmind_workers.ftr.data.quality import validate_ohlcv

logger = structlog.get_logger(__name__)

# Binance launched spot trading 2017-07; BTC/USDT history starts 2017-08.
_HISTORY_START = datetime(2017, 8, 1, tzinfo=UTC)


def _qa_and_dump(series, qa_dir: Path, cross_close=None) -> None:  # type: ignore[no-untyped-def]
    _, report = validate_ohlcv(
        series.frame,
        exchange=series.exchange,
        symbol=series.symbol,
        timeframe=series.timeframe,
        cross_venue_close=cross_close,
    )
    qa_dir.mkdir(parents=True, exist_ok=True)
    safe = series.symbol.replace("/", "_")
    out = qa_dir / f"{series.exchange}_{safe}_{series.timeframe}.json"
    row = report.to_row()
    row["first_ts"] = report.first_ts.isoformat() if report.first_ts else None
    row["last_ts"] = report.last_ts.isoformat() if report.last_ts else None
    out.write_text(json.dumps(row, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-1m", action="store_true", help="skip the 1m pull")
    parser.add_argument("--days-1m", type=int, default=None, help="override 1m history depth")
    parser.add_argument(
        "--symbols", type=str, default=None, help="comma-separated subset override"
    )
    args = parser.parse_args()

    settings = get_ftr_settings()
    cache_dir = settings.cache_dir
    qa_dir = settings.artifacts_dir / "qa"
    now = datetime.now(UTC)
    symbols = (
        [s.strip() for s in args.symbols.split(",")] if args.symbols else list(UNIVERSE_SUPERSET)
    )

    # Universe feasibility check: must be a live spot pair on a
    # uk_execution_feasible venue. Kraken lists majors as XXX/USD.
    kraken_equivalents = {s: s.split("/")[0] + "/USD" for s in symbols}
    listing = verify_spot_listing("kraken", list(kraken_equivalents.values()))
    dropped = [s for s, k in kraken_equivalents.items() if not listing.get(k, False)]
    kept = [s for s in symbols if s not in dropped]
    if dropped:
        logger.warning("ftr_universe_dropped", dropped=dropped, reason="not spot-listed on Kraken")
    universe_path = settings.artifacts_dir / "universe_feasibility.json"
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.write_text(
        json.dumps(
            {
                "checked_at": now.isoformat(),
                "feasible_venue_checked": "kraken",
                "kept": kept,
                "dropped": dropped,
            },
            indent=2,
        )
    )

    # Cross-venue sanity series first (used by QA of the primary series).
    cross = get_ohlcv(
        settings.cross_venue_exchange,
        settings.cross_venue_symbol,
        "1h",
        _HISTORY_START,
        now,
        cache_dir=cache_dir,
    )
    _qa_and_dump(cross, qa_dir)

    for sym in kept:
        for tf in ("1h", "4h"):
            series = get_ohlcv(
                settings.research_exchange, sym, tf, _HISTORY_START, now, cache_dir=cache_dir
            )
            cross_close = (
                cross.frame["close"]
                if (sym == settings.primary_symbol and tf == "1h")
                else None
            )
            _qa_and_dump(series, qa_dir, cross_close)
            logger.info("ftr_fetched", symbol=sym, timeframe=tf, rows=len(series.frame))

    if not args.skip_1m:
        days = args.days_1m or settings.minute_history_days
        start_1m = now - timedelta(days=days)
        for sym in kept:
            series = get_ohlcv(
                settings.research_exchange, sym, "1m", start_1m, now, cache_dir=cache_dir
            )
            _qa_and_dump(series, qa_dir)
            logger.info("ftr_fetched_1m", symbol=sym, rows=len(series.frame))

    logger.info("ftr_fetch_all_done", universe=kept, qa_dir=str(qa_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
