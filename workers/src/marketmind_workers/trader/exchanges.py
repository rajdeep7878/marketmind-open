"""Exchange adapters for the trader's market-data ingestion loop.

PAPER-SAFE BY CONSTRUCTION
--------------------------
Every adapter in this module exposes ONLY public market-data
endpoints — `fetch_recent_ohlcv` and `fetch_ohlcv_since`. Adapters
NEVER hold API keys, NEVER authenticate, and NEVER expose
order-placement methods.

This is a LOAD-BEARING v1 invariant. Adding live execution requires
a NEW adapter class, NOT an extension of an existing one. Extending
`BinanceAdapter` to add order-placement methods would silently
widen the trader's attack surface; with this rule, reviewers audit
"is this adapter authorised for live trading?" by inspecting the
class hierarchy, not by reading every method.

The `ExchangeAdapter` Protocol below is the structural contract
that the ingestion loop + tests depend on. New exchanges (Coinbase,
Kraken) implement this Protocol with their own concrete classes;
the same scoping rule — public market-data endpoints only —
applies to each.

RETRY POLICY
------------
3 attempts on transient `NetworkError` / `ExchangeError` with
exponential backoff (1s, 2s — total bounded wait <= 3s). ccxt's
`enableRateLimit=True` handles per-second rate limits internally;
our retries cover episodic transient failures (DNS hiccup, single
5xx, a single connection reset). Permanent failures (auth,
symbol-not-listed) still raise after 3 attempts — those are
configuration bugs that should fail loudly.

REQUEST TIMEOUT
---------------
10 seconds. Defensive cap so a hung TCP connection doesn't block
the ingestion loop for ccxt's default ~60s.
"""

from __future__ import annotations

import os
import time
from typing import Any, Final, Protocol

import ccxt
import structlog
from marketmind_shared.schemas.strategy_spec import AssetClass

log = structlog.get_logger(__name__)


class IngestionError(Exception):
    """Raised when an adapter exhausts retries on a fetch."""


class ExchangeAdapter(Protocol):
    """Structural contract for every market-data adapter.

    Ingestion and tests depend on THIS protocol — not on
    `BinanceAdapter` directly — so a future Coinbase / Kraken
    adapter slots in without touching ingestion code or test
    fixtures. New adapters implement these two methods; that's it.

    The methods return ccxt's raw OHLCV format:
    ``[[ts_ms, open, high, low, close, volume], ...]``.
    """

    def fetch_recent_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = ...,
    ) -> list[list[float]]:
        """Fetch the most recent `limit` candles for the pair.

        Implementations may return up to `limit` rows. The final
        row MAY be the in-flight current bar; ingestion filters
        that out via close_ts vs now.
        """
        ...

    def fetch_ohlcv_since(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int = ...,
    ) -> list[list[float]]:
        """Fetch up to `limit` candles starting at `since_ms`.

        Used by the gap-backfill path.
        """
        ...


_MAX_RETRIES: Final[int] = 3
_RETRY_BACKOFF_BASE_S: Final[float] = 1.0
_REQUEST_TIMEOUT_MS: Final[int] = 10_000


def _make_binance_client() -> Any:
    """Construct a Binance spot client. Pulled out so tests can mock it
    cleanly via `patch.object(exchanges, '_make_binance_client', ...)`.

    `enableRateLimit=True`: ccxt throttles internally to stay under
    Binance's per-minute weight ceiling.
    `timeout=10_000`: 10 second cap.
    """
    return ccxt.binance(
        {
            "enableRateLimit": True,
            "timeout": _REQUEST_TIMEOUT_MS,
        },
    )


class BinanceAdapter:
    """Thin retry wrapper around `ccxt.binance.fetch_ohlcv`.

    Implements `ExchangeAdapter` structurally — no explicit
    inheritance; pyright verifies the Protocol at every call site
    in `ingestion.py`. New exchanges should add a new class here
    (not extend this one — see the module docstring).

    The constructor accepts an optional pre-built client so tests
    can inject a mock without monkeypatching the module. Production
    paths construct via `_make_binance_client`.
    """

    def __init__(self, client: Any | None = None) -> None:
        self._client = client if client is not None else _make_binance_client()

    def fetch_recent_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
    ) -> list[list[float]]:
        """Fetch the most recent `limit` candles for the pair.

        Returns ccxt's raw OHLCV format:
        ``[[open_ts_ms, open, high, low, close, volume], ...]``.

        The most recent row MAY be the in-flight current bar; the
        ingestion loop filters that out via close_ts vs now — see
        `ingestion._filter_closed_candles`.
        """
        return self._fetch_with_retry(symbol, timeframe, since=None, limit=limit)

    def fetch_ohlcv_since(
        self,
        symbol: str,
        timeframe: str,
        since_ms: int,
        limit: int = 1000,
    ) -> list[list[float]]:
        """Fetch up to `limit` candles starting from `since_ms`.

        Used by the gap-backfill path. Returns ccxt's raw OHLCV
        format. Caller is responsible for trimming + upserting.
        """
        return self._fetch_with_retry(symbol, timeframe, since=since_ms, limit=limit)

    def _fetch_with_retry(
        self,
        symbol: str,
        timeframe: str,
        *,
        since: int | None,
        limit: int,
    ) -> list[list[float]]:
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                result = self._client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
                # ccxt returns list[list[Union[int, float]]] at the type
                # level; the cast is for pyright. At runtime each row is
                # always 6 elements: [ts_ms, o, h, l, c, v].
                return list(result)
            except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                last_exc = exc
                log.warning(
                    "binance_fetch_retry",
                    symbol=symbol,
                    timeframe=timeframe,
                    attempt=attempt + 1,
                    max_attempts=_MAX_RETRIES,
                    error=str(exc),
                )
                if attempt < _MAX_RETRIES - 1:
                    # Exponential backoff: 1s, 2s. Total bounded wait <= 3s.
                    time.sleep(_RETRY_BACKOFF_BASE_S * (2**attempt))
        raise IngestionError(
            f"binance fetch_ohlcv failed for {symbol} {timeframe} "
            f"after {_MAX_RETRIES} attempts: {last_exc}",
        ) from last_exc


# ===========================================================================
# Phase C C.1.4 (2026-05-26): factory dispatch on AssetClass
# ===========================================================================
#
# Production ingestion in `ingestion.py` was hardcoded to BinanceAdapter()
# for every (symbol, timeframe) pair before C.1.4. The factory below
# dispatches on `AssetClass` so the ingestion loop can route different
# asset classes to different concrete adapters.
#
# Symbol → asset_class inference: a small registry maps the symbol-string
# convention to the right class. The trader's `TRADER_SYMBOLS` env var
# carries ccxt-style strings (BTC/USDT, EUR/USD, XAU/USD, SPY, AAPL…) so
# inference is structural rather than configuration-driven.
#
# Phase C invariant: TRADER_SYMBOLS deployments should be homogeneous per
# asset class for now — the ingestion loop holds ONE adapter per cycle
# (selected from the first symbol's inferred class). Mixed-asset-class
# deployments arrive when C.5/C.6/C.7 lands the multi-class loop.

# Known asset-class assignments by symbol convention. Updated as new
# venues come online in C.8 / C.9.
_CRYPTO_QUOTES: Final[frozenset[str]] = frozenset({"USDT", "USDC", "BTC", "ETH"})
_FX_MAJOR_BASES: Final[frozenset[str]] = frozenset(
    {"EUR", "GBP", "AUD", "NZD", "USD", "JPY", "CHF", "CAD"},
)
_METALS_BASES: Final[frozenset[str]] = frozenset({"XAU", "XAG", "XPT", "XPD"})
_KNOWN_EQUITY_ETFS: Final[frozenset[str]] = frozenset(
    {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VXX"},
)


def infer_asset_class_from_symbol(symbol: str) -> AssetClass:
    """Map a ccxt-style symbol string to its AssetClass.

    Conventions:
      - "BASE/QUOTE" with QUOTE ∈ {USDT, USDC, BTC, ETH} → crypto_spot
      - "BASE/QUOTE" with BASE ∈ {EUR, GBP, ...} and QUOTE ∈ same set
        → fx_spot
      - "XAU/USD", "XAG/USD" etc. → metals_spot
      - bare ticker (no slash) matching a known ETF set → equity_etf
      - bare ticker otherwise (no slash) → equity_single
      - anything else → raises ValueError naming the symbol

    Phase C C.1.4 ships this as a deterministic registry. C.5 / C.6
    may add an explicit override on `Instrument` for ambiguous cases
    (e.g., a synthetic basket symbol that doesn't follow these
    conventions).
    """
    if "/" in symbol:
        base, _, quote = symbol.partition("/")
        if quote in _CRYPTO_QUOTES:
            return "crypto_spot"
        if base in _METALS_BASES:
            return "metals_spot"
        if base in _FX_MAJOR_BASES and quote in _FX_MAJOR_BASES:
            return "fx_spot"
        raise ValueError(
            f"infer_asset_class_from_symbol: cannot classify {symbol!r} — "
            f"base={base!r} quote={quote!r} match no known pattern",
        )
    # Bare tickers (no slash) — equity convention.
    if symbol in _KNOWN_EQUITY_ETFS:
        return "equity_etf"
    if symbol.isalpha() and symbol.isupper() and 1 <= len(symbol) <= 5:
        return "equity_single"
    raise ValueError(
        f"infer_asset_class_from_symbol: cannot classify {symbol!r} — "
        "not a ccxt-style pair and not a recognised equity ticker",
    )


def make_adapter(asset_class: AssetClass) -> ExchangeAdapter:
    """Construct the concrete ExchangeAdapter for `asset_class`.

    Phase C C.1.4 wires three branches:
      - crypto_spot → BinanceAdapter (production-tested since Phase A)
      - fx_spot     → OandaAdapter (C.1.3, cassette-tested)
      - metals_spot → OandaAdapter (same Oanda v20 endpoint)

    The two equity classes raise NotImplementedError naming the
    offending asset_class — they land in C.1.x or later when the
    AlpacaAdapter ships.

    For Oanda-backed adapters, credentials are read from env vars
    documented at `docs/deployment/env-vars.md` (the
    "Trader Oanda adapter" subsection added in C.1.3(7)):
      OANDA_API_KEY, OANDA_ACCOUNT_ID, OANDA_ENVIRONMENT
    OANDA_ENVIRONMENT MUST be `"practice"` in Phase C; the
    OandaAdapter constructor enforces this and raises IngestionError
    immediately on any other value.
    """
    if asset_class == "crypto_spot":
        return BinanceAdapter()
    if asset_class in ("fx_spot", "metals_spot"):
        # Local import keeps the BinanceAdapter-only fast path free
        # of the httpx import that exchanges_oanda pulls in.
        from marketmind_workers.trader.exchanges_oanda import OandaAdapter

        api_key = os.environ.get("OANDA_API_KEY", "")
        account_id = os.environ.get("OANDA_ACCOUNT_ID", "")
        environment = os.environ.get("OANDA_ENVIRONMENT", "practice")
        if not api_key or not account_id:
            raise IngestionError(
                f"make_adapter: asset_class={asset_class!r} requires "
                "OANDA_API_KEY + OANDA_ACCOUNT_ID env vars (see "
                "docs/deployment/env-vars.md). Live-API smoke gated "
                "to Phase C.1.6 per design doc §10.4.",
            )
        if environment != "practice":
            # Forward-fail with the same paper-only message OandaAdapter
            # would raise — but earlier, before the constructor.
            raise IngestionError(
                f"make_adapter: OANDA_ENVIRONMENT={environment!r} rejected; "
                "Phase C is paper-only (practice only).",
            )
        return OandaAdapter(
            account_id=account_id,
            api_token=api_key,
            environment="practice",
        )
    # equity_etf / equity_single — not yet implemented.
    raise NotImplementedError(
        f"Adapter dispatch for asset_class={asset_class!r} not yet "
        "implemented; expected at C.1.x per Phase C design doc §C.1.",
    )


__all__ = [
    "BinanceAdapter",
    "ExchangeAdapter",
    "IngestionError",
    "infer_asset_class_from_symbol",
    "make_adapter",
]
