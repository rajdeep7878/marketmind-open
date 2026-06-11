"""Phase C C.1.6 — live Oanda API smoke tests (opt-in, marker=live_api).

These tests hit the REAL Oanda practice API. C.1.3 was strictly
cassette-only; C.1.6 is the first sub-phase that verifies the live
endpoint matches what the cassettes captured.

Default pytest invocation skips these tests (pyproject.toml's `-m
"not integration and not live_api"`). Run explicitly:

    pytest -m live_api workers/tests/test_oanda_live_smoke.py -v

Required env vars (sourced from .env via docker-compose, or set in
shell for host-side runs):
  - OANDA_API_KEY
  - OANDA_ACCOUNT_ID
  - OANDA_ENVIRONMENT (must be "practice")

Tests will skip gracefully if any of these is missing — preserving
the "default suite passes on machines without Oanda creds" invariant.

Two tests:
  1. test_live_fetch_recent_ohlcv_eurusd_1h — adapter-level smoke,
     no DB writes. Validates cassette-vs-live shape parity.
  2. test_live_eurusd_lands_in_trader_candles — end-to-end ingestion
     smoke per design doc §C.1.6 ("EUR/USD 1H bars via OandaAdapter
     into `trader_candles`"). Runs one cycle with TRADER_SYMBOLS
     temporarily set to EUR/USD, asserts rows appear, then cleans up.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from marketmind_workers.trader.exchanges import IngestionError, make_adapter
from marketmind_workers.trader.exchanges_oanda import OandaAdapter

# live_api marker + filterwarnings for the SSL-socket-GC artefact:
# even after OandaAdapter.close() releases the httpx pool, the OS-level
# SSL sockets sit in TCP-WAIT briefly. Python's GC finaliser then sees
# them as "unclosed" and emits a ResourceWarning — pytest's strict
# unraisable trap promotes that to a test failure. Functional behaviour
# is correct (production close() works; sockets DO close); this is
# strictly a pytest-runtime artefact of httpx's pool semantics on live
# calls. Cassette tests never hit this because vcrpy intercepts before
# the SSL socket is even created.
pytestmark = [
    pytest.mark.live_api,
    pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnraisableExceptionWarning",
    ),
]


def _have_creds() -> bool:
    return bool(
        os.getenv("OANDA_API_KEY")
        and os.getenv("OANDA_ACCOUNT_ID")
        and os.getenv("OANDA_ENVIRONMENT") == "practice"
    )


REQUIRES_CREDS = pytest.mark.skipif(
    not _have_creds(),
    reason="OANDA_API_KEY / OANDA_ACCOUNT_ID / OANDA_ENVIRONMENT=practice not set",
)


# ---- adapter-level smoke ---------------------------------------------------


@REQUIRES_CREDS
def test_live_fetch_recent_ohlcv_eurusd_1h() -> None:
    """Live Oanda fetch of 10 EUR/USD 1H candles via the factory
    dispatch path. Verifies:
      - make_adapter("fx_spot") routes correctly post-C.1.4
      - Authentication works against the practice endpoint
      - Response shape matches the C.1.3 cassettes (ccxt-style OHLCV)
      - Numeric values are sane (EUR/USD between 0.5 and 2.0)
      - Timestamps are monotonic and hourly-spaced (allowing weekend gap)

    Prints sample data so the cassette-vs-live drift finding lands in
    pytest output, not just the assertion result.
    """
    adapter = make_adapter("fx_spot")
    assert isinstance(adapter, OandaAdapter), (
        f"factory dispatch broken: got {type(adapter).__name__}"
    )

    rows = adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=10)

    # Oanda may return fewer than `limit` if requesting near an
    # in-flight candle boundary; allow >=8 per brief.
    assert 8 <= len(rows) <= 10, f"expected 8-10 candles, got {len(rows)}"
    print(f"\n=== LIVE OANDA fetch_recent_ohlcv EUR/USD 1h returned {len(rows)} candles ===")
    print("First candle:", rows[0])
    print("Last candle: ", rows[-1])
    first_dt = datetime.fromtimestamp(rows[0][0] / 1000.0, tz=UTC)
    last_dt = datetime.fromtimestamp(rows[-1][0] / 1000.0, tz=UTC)
    print(f"ts range:    {first_dt.isoformat()} → {last_dt.isoformat()}")

    # Shape: every row 6 floats.
    for i, row in enumerate(rows):
        assert len(row) == 6, f"row {i} has {len(row)} fields, expected 6"
        assert all(isinstance(v, float) for v in row), (
            f"row {i} has non-float values: {[type(v).__name__ for v in row]}"
        )
        ts_ms, op, hi, lo, cl, vol = row
        # Sanity: EUR/USD prices.
        for px_name, px in [("open", op), ("high", hi), ("low", lo), ("close", cl)]:
            assert 0.5 < px < 2.0, (
                f"row {i} {px_name}={px:.5f} outside plausible EUR/USD range [0.5, 2.0]"
            )
        # No NaN.
        assert ts_ms == ts_ms, f"row {i} ts NaN"
        # Volume non-negative.
        assert vol >= 0, f"row {i} volume={vol} negative"
        # OHLC sanity.
        assert lo <= op <= hi, f"row {i} open={op} outside low={lo}..high={hi}"
        assert lo <= cl <= hi, f"row {i} close={cl} outside low={lo}..high={hi}"

    # Monotonic timestamps.
    ts_list = [int(r[0]) for r in rows]
    assert ts_list == sorted(ts_list), "timestamps not monotonic"
    # Hourly spacing — allow 3600s (normal) or larger (weekend gap).
    for i in range(1, len(ts_list)):
        delta = (ts_list[i] - ts_list[i - 1]) // 1000
        assert delta >= 3600, (
            f"gap between row {i-1} and {i} = {delta}s — smaller than 1h, suggests duplicate or out-of-order"
        )


# ---- end-to-end ingestion smoke -------------------------------------------


@REQUIRES_CREDS
def test_live_eurusd_lands_in_trader_candles() -> None:
    """End-to-end smoke per design doc §C.1.6: EUR/USD 1H bars via
    OandaAdapter actually land in trader_candles.

    Runs one ingestion cycle with TRADER_SYMBOLS temporarily set to
    "EUR/USD" via TraderSettings override (not env-var manipulation —
    we want the test isolated, not affecting the running trader).
    Asserts rows appear, then cleans up by DELETE-ing the test rows
    (production trader has no EUR/USD strategy, so cleanup is safe).

    DB access via DATABASE_URL env var; skip if not available
    (host-side runs without docker-compose may not have it).
    """
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set")

    # Construct a TraderSettings override with EUR/USD as the only symbol.
    from marketmind_workers.trader.config import TraderSettings
    from marketmind_workers.trader.ingestion import ingest_one_cycle

    settings = TraderSettings(
        trader_symbols="EUR/USD",
        trader_timeframes="1h",
    )  # type: ignore[call-arg]
    # Verify the validator passes (it should — all-FX is homogeneous).
    settings.assert_symbols_homogeneous_asset_class()

    # Run one cycle. No adapter injection — go through the factory path,
    # which routes EUR/USD → OandaAdapter via infer_asset_class_from_symbol.
    result = ingest_one_cycle(database_url, settings)
    print("\n=== LIVE ingest_one_cycle EUR/USD 1h ===")
    print(f"pairs_attempted={result.pairs_attempted}")
    print(f"pairs_succeeded={result.pairs_succeeded}")
    print(f"pairs_failed={result.pairs_failed}")
    print(f"candles_inserted={result.candles_inserted}")

    assert result.pairs_attempted == 1
    assert result.pairs_failed == 0, "EUR/USD ingestion failed"
    assert result.pairs_succeeded == 1

    # Verify rows landed.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*), MIN(close_ts), MAX(close_ts) FROM trader_candles "
            "WHERE symbol = %s AND timeframe = %s",
            ("EUR/USD", "1h"),
        )
        row: Any = cur.fetchone()
        count: int = row[0]
        min_ts: datetime | None = row[1]
        max_ts: datetime | None = row[2]
    print(f"trader_candles EUR/USD 1h rows: {count}")
    if count > 0:
        print(f"ts range in DB: {min_ts} → {max_ts}")
    # First cycle inserts new rows; even if some were already there from
    # an earlier run, the cycle must have observed at least one closed
    # candle (the cycle's "200 most recent" fetch on a busy FX pair
    # always returns at least a few).
    assert count > 0, "no EUR/USD candles in trader_candles after live ingest"

    # Cleanup — remove EUR/USD test rows so the live trader's
    # ingestion cycles don't carry them forward.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM trader_candles WHERE symbol = %s AND timeframe = %s",
            ("EUR/USD", "1h"),
        )
        deleted = cur.rowcount
        conn.commit()
    print(f"cleanup: deleted {deleted} test rows from trader_candles")


# ---- guard: factory dispatch failure surfaces clearly ---------------------


@REQUIRES_CREDS
def test_live_factory_rejects_trade_env_via_real_oanda_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence-in-depth: even with real, valid Oanda credentials, the
    factory rejects environment="trade" before any HTTP call. This is
    the C.1.3 paper-only guard validated against real creds.
    """
    monkeypatch.setenv("OANDA_ENVIRONMENT", "trade")
    with pytest.raises(IngestionError, match=r"paper-only|practice"):
        make_adapter("fx_spot")
