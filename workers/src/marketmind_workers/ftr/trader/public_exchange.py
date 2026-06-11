"""PublicOnlyExchange — ccxt wrapper constructed with NO API keys.

Whitelists public market-data methods; any other attribute access raises
``PaperOnlyViolation``. This is safety by construction, not by configuration:
there is no constructor parameter through which credentials could arrive.
"""

from __future__ import annotations

from typing import Any

import ccxt  # type: ignore[import-untyped]

from marketmind_workers.ftr.trader.execution_mode import PaperOnlyViolation

_PUBLIC_METHODS: frozenset[str] = frozenset(
    {
        "fetch_ohlcv",
        "fetch_ticker",
        "fetch_tickers",
        "fetch_order_book",
        "fetch_trades",
        "load_markets",
        "market",
        "markets",
        "fetch_time",
        "fetch_status",
    }
)


class PublicOnlyExchange:
    """Public-endpoint-only facade over a keyless ccxt client."""

    def __init__(self, exchange: str) -> None:
        cls = getattr(ccxt, exchange)
        # Constructed WITHOUT apiKey/secret on purpose — there is no code
        # path that supplies credentials.
        self._sdk = cls({"enableRateLimit": True, "timeout": 15_000})
        self._exchange = exchange

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in _PUBLIC_METHODS:
            raise PaperOnlyViolation(
                f"{name!r} is not a whitelisted public method on PublicOnlyExchange "
                f"({self._exchange}); FTR has no private/authenticated API access."
            )
        return getattr(self._sdk, name)
