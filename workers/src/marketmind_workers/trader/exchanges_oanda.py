"""Oanda fxTrade adapter for FX + metals market data (Phase C C.1.3).

PAPER-SAFE BY CONSTRUCTION
--------------------------
Identical invariant to `exchanges.py`'s BinanceAdapter: this adapter
exposes ONLY public market-data endpoints (`fetch_recent_ohlcv` and
`fetch_ohlcv_since`). NEVER holds order-placement methods. NEVER
authenticates against the live `trade` environment — the constructor
asserts `environment == "practice"` and raises immediately on any
other value, BEFORE any HTTP call is attempted.

This is the C.1.3 implementation of the cred-gate sketched in the
Phase C design doc §10.4. Live execution (Phase D) requires a
NEW adapter class, NOT an extension of this one.

CASSETTE-ONLY in C.1.3
----------------------
Per design doc §10.4, Oanda paper-account credentials are NOT
provisioned during C.1.3. All unit tests replay from VCR cassettes
under `workers/tests/cassettes/oanda/`. The live-API smoke test
lands at C.1.6, once credentials are available.

RETRY POLICY
------------
Mirrors BinanceAdapter exactly: 3 attempts on transient HTTP errors
with exponential backoff (1s, 2s — total bounded wait ≤ 3s). Auth
failures (401), bad symbols (404), and other permanent errors raise
after the first attempt; they are configuration bugs that should
fail loudly. Rate-limit responses (429) honour `Retry-After` if
present, otherwise fall back to the same exponential schedule.

REQUEST TIMEOUT
---------------
10 seconds. Matches BinanceAdapter's defensive cap.

ENDPOINTS
---------
Practice base URL: https://api-fxpractice.oanda.com
Live base URL (REJECTED at adapter init): https://api-fxtrade.oanda.com

Candles endpoint:
    GET /v3/instruments/{instrument}/candles
        ?granularity=H4         (Oanda's timeframe code; mapped from
                                 the codebase's "4h" convention)
        &count=200              (when fetching recent)
        &from={iso8601}         (when paginating from a timestamp)
        &price=M                (midpoint OHLC — Oanda offers bid/
                                 ask/mid; we use mid for backtests)
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, Final, Literal

import httpx
import structlog

from marketmind_workers.trader.exchanges import ExchangeAdapter, IngestionError

log = structlog.get_logger(__name__)


# Oanda granularity codes — uppercase, with a few quirks (M1 for 1-minute
# is the same code Oanda uses; M for monthly is also "M" so we don't
# expose monthly here). Sourced from Oanda's v20 REST docs.
_OANDA_GRANULARITY: Final[dict[str, str]] = {
    "1m": "M1",
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "4h": "H4",
    "1d": "D",
}

# vcrpy-friendly: practice endpoint is the only one this adapter ever
# touches. The "trade" endpoint exists but is REJECTED at adapter init.
_OANDA_PRACTICE_BASE_URL: Final[str] = "https://api-fxpractice.oanda.com"
_OANDA_TRADE_BASE_URL: Final[str] = "https://api-fxtrade.oanda.com"

_MAX_RETRIES: Final[int] = 3
_RETRY_BACKOFF_BASE_S: Final[float] = 1.0
_REQUEST_TIMEOUT_S: Final[float] = 10.0

# Pagination page size for fetch_ohlcv_since. Oanda's server cap is
# 5000 per request, but smaller pages reduce blast radius on a
# retry (one failed page costs less than 5000-candles wasted) and
# play more nicely with rate limits.
_PAGE_SIZE: Final[int] = 500

# Bar duration in epoch ms — used to advance the `from` cursor in
# pagination by exactly one bar past the last returned candle.
_BAR_DURATION_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _bar_duration_ms(timeframe: str) -> int:
    try:
        return _BAR_DURATION_MS[timeframe]
    except KeyError as exc:
        raise IngestionError(
            f"Oanda adapter: unsupported timeframe {timeframe!r} for "
            f"pagination cursor advance",
        ) from exc


# Oanda symbol convention: EUR_USD (underscore), XAU_USD, etc.
# The codebase's convention is EUR/USD (slash) to match ccxt.
# This helper converts on the way out.
def _to_oanda_symbol(symbol: str) -> str:
    """Map ccxt-style "EUR/USD" to Oanda's "EUR_USD". Leaves a string
    already in Oanda form unchanged.
    """
    return symbol.replace("/", "_") if "/" in symbol else symbol


def _granularity(timeframe: str) -> str:
    try:
        return _OANDA_GRANULARITY[timeframe]
    except KeyError as exc:
        raise IngestionError(
            f"Oanda adapter: unsupported timeframe {timeframe!r}. "
            f"Supported: {sorted(_OANDA_GRANULARITY.keys())}",
        ) from exc


class OandaAdapter:
    """Read-only Oanda v20 fxTrade market-data adapter.

    Implements `ExchangeAdapter` structurally — no explicit
    inheritance; pyright + the protocol-conformance unit test verify
    the structural match. Used by the trader's ingestion loop in
    Phase C for the `fx_spot` and `metals_spot` asset classes; the
    factory dispatch (C.1.4) routes Instrument.asset_class to the
    correct adapter.

    Constructor parameters:
      account_id: Oanda account ID (e.g. "101-001-1234567-001")
      api_token: Oanda API token. NEVER logged, NEVER serialised.
      environment: MUST be "practice" in Phase C — "trade" raises
        immediately. C.1.3 paper-only gate.
      client: Optional pre-built httpx.Client for test injection.
        Production code constructs internally; tests pass a Client
        whose transport is wired to a vcrpy cassette.
    """

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        environment: Literal["practice", "trade"] = "practice",
        client: httpx.Client | None = None,
    ) -> None:
        # PAPER-ONLY GATE: this is the first thing the adapter does.
        # Any non-practice value raises BEFORE any HTTP call or even
        # client construction is attempted. Phase D will add a separate
        # class for "trade"; extending this one is forbidden.
        if environment != "practice":
            raise IngestionError(
                "OandaAdapter rejects environment="
                f"{environment!r}: Phase C is paper-only. Live "
                "trading requires a separate adapter class (see "
                "exchanges.py module docstring 'PAPER-SAFE BY "
                "CONSTRUCTION').",
            )
        if not account_id:
            raise IngestionError("OandaAdapter: account_id is required")
        if not api_token:
            raise IngestionError("OandaAdapter: api_token is required")

        self._account_id = account_id
        self._api_token = api_token
        self._environment = environment
        self._base_url = _OANDA_PRACTICE_BASE_URL
        self._client = client if client is not None else self._make_client()

    def _make_client(self) -> httpx.Client:
        """Construct the default httpx.Client. Test code passes its
        own Client (with vcrpy transport) via the `client=` kwarg.
        """
        return httpx.Client(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {self._api_token}",
                "Accept-Datetime-Format": "RFC3339",
            },
            timeout=_REQUEST_TIMEOUT_S,
        )

    def close(self) -> None:
        """Release the httpx.Client's underlying connection pool.

        C.1.6 finding (2026-05-26): cassette tests never exercised
        connection lifecycle because vcrpy intercepts at the transport
        layer before httpx opens a real socket. Live calls open SSL
        sockets that leak as ResourceWarning until GC reaps them.
        Production cycles (60s+ apart) wouldn't accumulate FDs in
        practice, but pytest's strict warning mode catches the GC's
        unraisable warning, and an explicit close is cleaner anyway.
        """
        self._client.close()

    def __enter__(self) -> OandaAdapter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- ExchangeAdapter Protocol --------------------------------------

    def fetch_recent_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> list[list[float]]:
        """Fetch the most recent `limit` candles for the pair.

        Returns ccxt-compatible OHLCV:
            [[open_ts_ms, open, high, low, close, volume], ...]

        Volume on Oanda is the candle's tick count (not currency
        volume — Oanda doesn't publish FX volume). Sufficient for
        the trader's volume-aware indicators (VolumeSMA, OBV) since
        they're scale-invariant.

        Implementation lands in C.1.3(3). Shell-only at C.1.3(2).
        """
        params = {
            "granularity": _granularity(timeframe),
            "count": str(limit),
            "price": "M",
        }
        return self._fetch_candles_with_retry(symbol, params)

    def fetch_ohlcv_since(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int = 1000,
    ) -> list[list[float]]:
        """Fetch up to `limit` candles starting at `since_ms`.

        Used by the gap-backfill path. Oanda paginates internally
        via the `from + count` convention — `count` per request caps
        at 5000 server-side but we use a smaller page size for
        memory + retry-blast-radius reasons. This method loops until
        EITHER the accumulated total reaches `limit` OR the server
        returns fewer than `_PAGE_SIZE` candles (end of available
        data).

        Returns ccxt-shape OHLCV: [[ts_ms, o, h, l, c, v], ...]
        — a single contiguous series with no duplicates and no
        gaps (assuming the underlying data has no gaps).
        """
        granularity = _granularity(timeframe)
        bar_ms = _bar_duration_ms(timeframe)
        accumulated: list[list[float]] = []
        current_since_ms = since_ms
        # Defensive loop cap: even with bar_ms=60_000 (1m) and
        # limit=1_000_000, the loop terminates after ~334 pages.
        # The cap stops at 10_000 to keep an obviously-pathological
        # response (empty page returned with non-zero `from`) from
        # spinning forever.
        max_pages = 10_000
        for _ in range(max_pages):
            remaining = limit - len(accumulated)
            if remaining <= 0:
                break
            page_count = min(_PAGE_SIZE, remaining)
            since_iso = datetime.fromtimestamp(current_since_ms / 1000.0, tz=UTC).isoformat()
            params = {
                "granularity": granularity,
                "from": since_iso,
                "count": str(page_count),
                "price": "M",
            }
            page = self._fetch_candles_with_retry(symbol, params)
            if not page:
                break
            accumulated.extend(page)
            # End-of-data heuristic: a short page means the server
            # had nothing more to give. Stop polling.
            if len(page) < page_count:
                break
            # Advance: next page starts one bar after the LAST
            # candle in this page (avoiding the duplicate-edge
            # condition where Oanda might re-return the boundary
            # candle).
            last_ts_ms = int(page[-1][0])
            current_since_ms = last_ts_ms + bar_ms
        return accumulated[:limit]

    # ---- internal ------------------------------------------------------

    def _fetch_candles_with_retry(
        self,
        symbol: str,
        params: dict[str, str],
    ) -> list[list[float]]:
        """3-attempt retry around the candles endpoint. Pattern mirrors
        BinanceAdapter._fetch_with_retry. Implementation lands in
        C.1.3(3); placeholder raises NotImplementedError until then so
        any premature caller fails loudly rather than returning
        ambiguous empty data.
        """
        oanda_symbol = _to_oanda_symbol(symbol)
        path = f"/v3/instruments/{oanda_symbol}/candles"
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.get(path, params=params)
                if response.status_code == 200:
                    try:
                        return _parse_candles(response.json())
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        # Malformed body — surface as typed error, no
                        # retry. A 200 with non-JSON body is almost
                        # always a transparent-proxy / WAF intercept
                        # that retry won't fix.
                        raise IngestionError(
                            f"Oanda candles malformed response for "
                            f"{oanda_symbol}: {type(exc).__name__}: {exc} "
                            f"(body head: {response.text[:120]!r})",
                        ) from exc
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "?")
                    raise IngestionError(
                        f"Oanda rate-limited (HTTP 429) for {oanda_symbol}; "
                        f"Retry-After={retry_after}s. Body: "
                        f"{response.text[:200]}",
                    )
                # Auth (401/403), bad symbol (404), other 4xx/5xx →
                # typed error, no retry. These are permanent config
                # bugs that should fail loudly.
                raise IngestionError(
                    f"Oanda candles HTTP {response.status_code} "
                    f"for {oanda_symbol}: {response.text[:200]}",
                )
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                log.warning(
                    "oanda_fetch_retry",
                    symbol=oanda_symbol,
                    attempt=attempt + 1,
                    max_attempts=_MAX_RETRIES,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF_BASE_S * (2**attempt))
            except IngestionError:
                # Already typed — surface immediately, no retry.
                raise
        raise IngestionError(
            f"Oanda fetch_candles failed for {oanda_symbol} after "
            f"{_MAX_RETRIES} attempts: {last_exc}",
        ) from last_exc


def _parse_candles(payload: dict[str, Any]) -> list[list[float]]:
    """Convert Oanda's candles payload to ccxt's OHLCV shape.

    Oanda shape:
        {"candles": [{"time": "2024-01-01T00:00:00.000000Z",
                      "mid": {"o": "1.10", "h": "1.11", "l": "1.09",
                              "c": "1.10"},
                      "volume": 1234,
                      "complete": true}, ...]}

    Output: [[ts_ms, open, high, low, close, volume], ...] — same
    shape ccxt returns from Binance, so downstream consumers don't
    fork based on adapter.

    Skips `complete=false` candles (Oanda flags in-flight bars; we
    don't need to filter them downstream because ingestion already
    has its own close_ts vs now check, but skipping here makes the
    adapter output cleaner).
    """
    candles_in = payload.get("candles", [])
    out: list[list[float]] = []
    for c in candles_in:
        if not c.get("complete", True):
            continue
        ts_iso = c["time"]
        # RFC3339: "2024-01-01T00:00:00.000000Z" → epoch ms.
        ts_ms = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000.0)
        mid = c["mid"]
        out.append(
            [
                float(ts_ms),
                float(mid["o"]),
                float(mid["h"]),
                float(mid["l"]),
                float(mid["c"]),
                float(c.get("volume", 0)),
            ],
        )
    return out


# Re-export ExchangeAdapter for structural-conformance tests that import
# from this module rather than exchanges.py.
__all__ = ["ExchangeAdapter", "OandaAdapter"]
