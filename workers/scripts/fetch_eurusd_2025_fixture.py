"""Phase C C.5(3) — one-shot fetch of EUR/USD 1H bars for 2025.

Builds the test fixture `tests/fixtures/market/eurusd_1h_2025.parquet`
from a live Oanda practice-API call. Persists to disk for offline CI
reproducibility — same pattern as Phase B's BTC fixtures
(tests/fixtures/market/btc_usdt_{4h,1h,15m}.parquet).

USAGE
-----

  cd <repo-root>
  uv run python workers/scripts/fetch_eurusd_2025_fixture.py

Required env vars (read directly from process env; .env-loaded vars
work via `docker compose run` or host-side `direnv` / manual export):
  OANDA_API_KEY      — Oanda practice bearer token
  OANDA_ACCOUNT_ID   — practice account id (101-001-XXXXXXX-001)
  OANDA_ENVIRONMENT  — defaults to "practice"; "trade" rejected

OUTPUT
------

  tests/fixtures/market/eurusd_1h_2025.parquet
    - ~6200 rows (FX 24/5: 250 trading days × ~24 1H bars + holidays)
    - Columns: open, high, low, close, volume (Oanda tick count)
    - Index: DatetimeIndex tz-aware UTC, ascending
    - Estimated size: ~300-500 KB (well under the 1 MB cap)

RE-RUN POLICY
-------------

  This is a ONE-SHOT script. 2025 is fully in the past; the fixture is
  permanent reference data. Re-running produces a byte-identical file
  (modulo Oanda's data-provider quirks — they shouldn't backfill 2025
  candles). Re-run only if:
    - Oanda corrects historical data (rare; surfaces as a backtest
      regression and triggers re-investigation)
    - The fixture file gets corrupted or accidentally deleted
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from marketmind_workers.trader.exchanges_oanda import OandaAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_PATH = REPO_ROOT / "tests" / "fixtures" / "market" / "eurusd_1h_2025.parquet"


def main() -> int:
    api_key = os.environ.get("OANDA_API_KEY", "")
    account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
    environment = os.environ.get("OANDA_ENVIRONMENT", "practice")

    if not api_key or not account_id:
        print(
            "ERROR: OANDA_API_KEY + OANDA_ACCOUNT_ID env vars required. "
            "See docs/deployment/env-vars.md.",
            file=sys.stderr,
        )
        return 1
    if environment != "practice":
        print(
            f"ERROR: OANDA_ENVIRONMENT={environment!r} rejected; Phase C "
            "is paper-only (practice only).",
            file=sys.stderr,
        )
        return 1

    print(f"=== fetch_eurusd_2025_fixture — output: {OUTPUT_PATH} ===")
    adapter = OandaAdapter(
        account_id=account_id,
        api_token=api_key,
        environment="practice",
    )

    # 2025-01-01 00:00 UTC start. Oanda's first bar will land at or
    # after this timestamp; FX session opens Sunday 22:00 UTC the
    # week before, but Jan 1 2025 is a Wednesday so the fetch covers
    # the full year cleanly.
    since_ms = int(datetime(2025, 1, 1, 0, 0, tzinfo=UTC).timestamp() * 1000.0)
    # Hard cap at ~9000 rows so the fetch is bounded even if Oanda's
    # pagination overshoots. Full year FX 24/5 ≈ 6240 weekday rows.
    limit = 9000

    print(
        f"fetching EUR/USD 1H from "
        f"{datetime.fromtimestamp(since_ms / 1000, tz=UTC).isoformat()} (limit {limit})",
    )
    try:
        rows = adapter.fetch_ohlcv_since("EUR/USD", "1h", since_ms=since_ms, limit=limit)
    finally:
        adapter.close()
    print(f"fetched {len(rows)} candles")

    if not rows:
        print("ERROR: zero candles returned — check creds, network, date range", file=sys.stderr)
        return 1

    # ccxt-style [ts_ms, o, h, l, c, v] → DataFrame with DatetimeIndex.
    # pandas-stubs typing rejects `list[str]` for columns; pd.Index wraps cleanly.
    df = pd.DataFrame(
        rows,
        columns=pd.Index(["ts_ms", "open", "high", "low", "close", "volume"]),
    )
    df["timestamp"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.set_index("timestamp").drop(columns=["ts_ms"])
    df = df.sort_index()

    # Trim to 2025 only (Oanda may return one trailing 2026 bar if the
    # pagination cursor advances past Dec 31).
    df = df[df.index < pd.Timestamp("2026-01-01", tz="UTC")]

    print(f"final shape: {df.shape}")
    print(f"ts range: {df.index[0]} → {df.index[-1]}")
    weekday_count = int((df.index.weekday < 5).sum())  # type: ignore[attr-defined]
    weekend_count = int((df.index.weekday >= 5).sum())  # type: ignore[attr-defined]
    print(f"weekday count: {weekday_count}, weekend count: {weekend_count}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUTPUT_PATH, engine="pyarrow", compression="snappy")
    size_kb = OUTPUT_PATH.stat().st_size / 1024.0
    print(f"wrote {OUTPUT_PATH} ({size_kb:.1f} KB)")
    if size_kb > 1024:
        print(
            f"WARNING: fixture is {size_kb:.1f} KB, exceeds 1 MB soft cap. "
            "Consider git-lfs or trimming the date range.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
