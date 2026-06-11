"""Phase C C.1.3 — OandaAdapter unit tests.

EVERY test in this module replays from a VCR cassette under
`workers/tests/cassettes/oanda/`. Zero real HTTP calls. The
adapter's paper-only guard ensures the cassettes only ever capture
the practice endpoint; the `auth_token` baked into cassettes is
the deterministic dummy "DUMMY_API_TOKEN_FOR_CASSETTE".

Cred-provisioning gate per design doc §10.4: live-API smoke is
deferred to C.1.6. C.1.3 validates the adapter SHAPE.

Empirical-inspection (v1.2 META-PATTERN): cassette responses
were hand-crafted to match Oanda's documented v20 schema; every
expected value in the assertions was read from the cassette body
before being encoded as an assertion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
import vcr  # type: ignore[import-untyped]
from marketmind_workers.trader.exchanges import ExchangeAdapter, IngestionError
from marketmind_workers.trader.exchanges_oanda import OandaAdapter
from vcr.record_mode import RecordMode  # type: ignore[import-untyped]

CASSETTE_DIR: Final[Path] = Path(__file__).parent / "cassettes" / "oanda"


def _vcr() -> Any:
    """Cassette config: replay-only, never record. Strict request
    matching (method + URL + query params), so any drift in the
    adapter's outbound request makes the test fail loudly with a
    "cassette not played" error rather than silently going to the
    network.
    """
    return vcr.VCR(
        cassette_library_dir=str(CASSETTE_DIR),
        record_mode=RecordMode.NONE,
        match_on=["method", "scheme", "host", "path", "query"],
    )


def _make_adapter() -> OandaAdapter:
    """Build an OandaAdapter wired for cassette replay.

    api_token matches the value baked into cassettes — kept stable
    so future tightening of match_on (to include Authorization
    header) doesn't require cassette rewrites.
    """
    return OandaAdapter(
        account_id="101-001-1234567-001",
        api_token="DUMMY_API_TOKEN_FOR_CASSETTE",
        environment="practice",
    )


# ---- happy path: fetch_recent_ohlcv ---------------------------------------


def test_fetch_recent_ohlcv_returns_ccxt_shape_ohlcv() -> None:
    """Cassette captures a 5-candle response for EUR/USD 1H. Adapter
    must return a list of 6-element float lists (ts_ms, o, h, l, c, v),
    excluding the in-flight (complete=false) last candle.
    """
    adapter = _make_adapter()
    cassette = "fetch_recent_ohlcv_eurusd_1h_200_candles.yaml"
    with _vcr().use_cassette(cassette):
        rows = adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=5)
    # 4 complete + 1 in-flight skipped → 4 returned.
    assert len(rows) == 4, f"expected 4 complete candles, got {len(rows)}"
    # Each row is ccxt-shape.
    for row in rows:
        assert len(row) == 6
        assert all(isinstance(v, float) for v in row)
    # First-row spot check against the cassette body.
    first = rows[0]
    # ts_ms = epoch ms of 2024-01-01T00:00:00Z = 1704067200000
    assert first[0] == 1704067200000.0
    # mid.o was "1.10410"
    assert first[1] == pytest.approx(1.10410, rel=1e-9)
    # mid.h "1.10520", mid.l "1.10380", mid.c "1.10500"
    assert first[2] == pytest.approx(1.10520, rel=1e-9)
    assert first[3] == pytest.approx(1.10380, rel=1e-9)
    assert first[4] == pytest.approx(1.10500, rel=1e-9)
    # volume = 1234
    assert first[5] == 1234.0


def test_fetch_recent_ohlcv_skips_in_flight_complete_false_candle() -> None:
    """Cassette includes a `complete: false` final candle (Oanda's
    convention for the in-flight bar). The adapter must drop it.
    """
    adapter = _make_adapter()
    with _vcr().use_cassette("fetch_recent_ohlcv_eurusd_1h_200_candles.yaml"):
        rows = adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=5)
    # The cassette's 5th candle (timestamp 04:00 UTC) is in-flight.
    # Verify the last returned row is the 03:00 UTC bar, not 04:00.
    last_ts_ms = rows[-1][0]
    # 2024-01-01T03:00:00Z = 1704078000000
    assert last_ts_ms == 1704078000000.0


def test_fetch_recent_ohlcv_symbol_translation_eurusd_to_oanda() -> None:
    """ccxt-style "EUR/USD" must be translated to Oanda's "EUR_USD"
    in the outbound request URL. The cassette URL includes EUR_USD;
    if the adapter sent EUR/USD verbatim, vcrpy's query-matching
    would refuse to play.
    """
    adapter = _make_adapter()
    with _vcr().use_cassette("fetch_recent_ohlcv_eurusd_1h_200_candles.yaml"):
        rows = adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=5)
    assert len(rows) > 0


# ---- error paths ----------------------------------------------------------


def test_auth_failure_401_raises_typed_error_no_retry() -> None:
    """Cassette returns HTTP 401. Adapter must raise IngestionError
    IMMEDIATELY (not a raw httpx exception, not retried 3 times) —
    auth is a permanent config bug that should fail loudly.
    """
    adapter = _make_adapter()
    with (
        _vcr().use_cassette("auth_failure_401.yaml"),
        pytest.raises(IngestionError, match=r"HTTP 401|Authorization"),
    ):
        adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=5)


def test_rate_limit_429_raises_typed_error_with_retry_after_surfaced() -> None:
    """Cassette returns HTTP 429 with Retry-After: 30. Adapter must
    raise IngestionError including the Retry-After value in the
    message so callers can act on it (e.g. trader can back off the
    whole symbol for `retry_after` seconds before the next cycle).
    """
    adapter = _make_adapter()
    with (
        _vcr().use_cassette("rate_limit_429.yaml"),
        pytest.raises(IngestionError, match=r"rate-limited|429|Retry-After=30") as exc_info,
    ):
        adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=5)
    # Retry-After value present in the exception message.
    assert "30" in str(exc_info.value)


def test_malformed_response_raises_typed_error_no_retry() -> None:
    """Cassette returns HTTP 200 with an HTML body (transparent proxy
    / WAF intercept simulation). Adapter must catch the JSON decode
    failure and raise IngestionError, NOT bubble up raw json.JSONDecodeError.
    """
    adapter = _make_adapter()
    with (
        _vcr().use_cassette("malformed_response.yaml"),
        pytest.raises(IngestionError, match=r"malformed response"),
    ):
        adapter.fetch_recent_ohlcv("EUR/USD", "1h", limit=5)


# ---- pagination: fetch_ohlcv_since ----------------------------------------


def test_fetch_ohlcv_since_paginated_assembles_contiguous_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cassette captures 3 pages × 3 candles (7 total — last page is
    a short page signaling end-of-data). Adapter must:
      - emit 3 sequential GETs with the correct `from=` cursors
      - assemble a single 7-row series
      - have no duplicates and no gaps
    Page size is overridden to 3 to keep the cassette readable.
    """
    from marketmind_workers.trader import exchanges_oanda
    monkeypatch.setattr(exchanges_oanda, "_PAGE_SIZE", 3)

    adapter = _make_adapter()
    # since_ms = 2024-01-01T00:00:00Z = 1704067200000
    since_ms = 1704067200000
    with _vcr().use_cassette("fetch_ohlcv_since_eurusd_1h_paginated.yaml"):
        rows = adapter.fetch_ohlcv_since("EUR/USD", "1h", since_ms=since_ms, limit=100)

    # 3 + 3 + 1 = 7 candles, all complete=true in the cassette.
    assert len(rows) == 7
    # Contiguity: each timestamp = previous + 1h (3_600_000 ms).
    timestamps = [int(r[0]) for r in rows]
    expected_starts = [
        1704067200000,  # 00:00
        1704070800000,  # 01:00
        1704074400000,  # 02:00
        1704078000000,  # 03:00
        1704081600000,  # 04:00
        1704085200000,  # 05:00
        1704088800000,  # 06:00
    ]
    assert timestamps == expected_starts
    # No duplicates (the cursor-advance trick).
    assert len(set(timestamps)) == len(timestamps)


def test_fetch_ohlcv_since_respects_limit_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If limit < total available, the loop stops at `limit`."""
    from marketmind_workers.trader import exchanges_oanda
    monkeypatch.setattr(exchanges_oanda, "_PAGE_SIZE", 3)

    adapter = _make_adapter()
    with _vcr().use_cassette("fetch_ohlcv_since_eurusd_1h_paginated.yaml"):
        # limit=3 — should make exactly one page request and stop.
        rows = adapter.fetch_ohlcv_since("EUR/USD", "1h", since_ms=1704067200000, limit=3)
    assert len(rows) == 3


def test_bar_duration_ms_matches_documented_table() -> None:
    """Spot-check: 1h = 3_600_000, 4h = 14_400_000, 1d = 86_400_000."""
    from marketmind_workers.trader.exchanges_oanda import (
        _bar_duration_ms,  # pyright: ignore[reportPrivateUsage]
    )
    assert _bar_duration_ms("1h") == 3_600_000
    assert _bar_duration_ms("4h") == 14_400_000
    assert _bar_duration_ms("1d") == 86_400_000


def test_bar_duration_ms_rejects_unsupported_timeframe() -> None:
    from marketmind_workers.trader.exchanges_oanda import (
        _bar_duration_ms,  # pyright: ignore[reportPrivateUsage]
    )
    with pytest.raises(IngestionError, match=r"unsupported timeframe"):
        _bar_duration_ms("3h")


# ---- structural conformance ------------------------------------------------


def test_oanda_adapter_satisfies_exchange_adapter_protocol() -> None:
    """OandaAdapter is structurally an ExchangeAdapter. Pyright checks
    the structural match statically; this test exercises it at
    runtime via duck-typing.
    """
    adapter = _make_adapter()
    assert callable(adapter.fetch_recent_ohlcv)
    assert callable(adapter.fetch_ohlcv_since)
    # Static check: assigning to an ExchangeAdapter-typed variable
    # would fail pyright if the structural match were broken.
    typed_as_protocol: ExchangeAdapter = adapter
    assert typed_as_protocol is adapter


# ---- paper-only guard ------------------------------------------------------


def test_environment_trade_raises_before_any_http_call() -> None:
    """The PAPER-SAFE BY CONSTRUCTION gate: environment="trade"
    raises IngestionError IMMEDIATELY at adapter __init__, before
    any HTTP client construction or request is attempted. This is
    the most important invariant of the entire C.1.3 sub-phase.
    """
    with pytest.raises(IngestionError, match=r"paper-only|practice|environment"):
        OandaAdapter(
            account_id="101-001-test",
            api_token="x",
            environment="trade",  # type: ignore[arg-type]
        )


def test_environment_practice_initialises_with_practice_base_url() -> None:
    """The accepted path: environment="practice" yields an adapter
    whose base URL is the practice endpoint (NOT the live trade URL).
    """
    adapter = OandaAdapter(
        account_id="101-001-test",
        api_token="x",
        environment="practice",
    )
    assert adapter._environment == "practice"  # pyright: ignore[reportPrivateUsage]
    assert adapter._base_url == "https://api-fxpractice.oanda.com"  # pyright: ignore[reportPrivateUsage]
    # Live URL constant exists in the module for documentation only;
    # this adapter instance must never carry it.
    assert "fxtrade" not in adapter._base_url  # pyright: ignore[reportPrivateUsage]


def test_account_id_empty_string_rejected() -> None:
    with pytest.raises(IngestionError, match=r"account_id"):
        OandaAdapter(account_id="", api_token="x", environment="practice")


def test_api_token_empty_string_rejected() -> None:
    with pytest.raises(IngestionError, match=r"api_token"):
        OandaAdapter(account_id="101-001-test", api_token="", environment="practice")


# ---- granularity mapping ---------------------------------------------------


@pytest.mark.parametrize(
    "tf,expected_granularity",
    [
        ("1m", "M1"),
        ("5m", "M5"),
        ("15m", "M15"),
        ("30m", "M30"),
        ("1h", "H1"),
        ("4h", "H4"),
        ("1d", "D"),
    ],
)
def test_supported_timeframes_map_to_oanda_granularity(tf: str, expected_granularity: str) -> None:
    """Documented timeframe → Oanda granularity codes per v20 REST."""
    from marketmind_workers.trader.exchanges_oanda import (
        _granularity,  # pyright: ignore[reportPrivateUsage]
    )
    assert _granularity(tf) == expected_granularity


def test_unsupported_timeframe_raises() -> None:
    from marketmind_workers.trader.exchanges_oanda import (
        _granularity,  # pyright: ignore[reportPrivateUsage]
    )
    with pytest.raises(IngestionError, match=r"unsupported timeframe"):
        _granularity("3h")


# ---- symbol translation ----------------------------------------------------


@pytest.mark.parametrize(
    "ccxt_symbol,oanda_symbol",
    [
        ("EUR/USD", "EUR_USD"),
        ("GBP/USD", "GBP_USD"),
        ("USD/JPY", "USD_JPY"),
        ("XAU/USD", "XAU_USD"),
        # Already in Oanda form: idempotent.
        ("EUR_USD", "EUR_USD"),
        ("XAU_USD", "XAU_USD"),
    ],
)
def test_symbol_translation_ccxt_to_oanda(ccxt_symbol: str, oanda_symbol: str) -> None:
    from marketmind_workers.trader.exchanges_oanda import (
        _to_oanda_symbol,  # pyright: ignore[reportPrivateUsage]
    )
    assert _to_oanda_symbol(ccxt_symbol) == oanda_symbol
