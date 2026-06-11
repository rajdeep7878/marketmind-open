"""Unit tests for BinanceAdapter's retry + fetch logic.

Uses a fake ccxt client (`_FlakyClient`) injected via the adapter's
optional `client=` arg. The fake records every call so we can verify
the retry count + the exact arguments forwarded to ccxt.

`time.sleep` is monkeypatched out to keep test runtime in ms instead
of seconds — the backoff schedule is verified via the sleep-duration
arguments instead of by waiting.
"""

from __future__ import annotations

from typing import Any

import ccxt
import pytest
from marketmind_workers.trader.exchanges import BinanceAdapter, IngestionError


class _FlakyClient:
    """Fake ccxt client. Raises NetworkError on the first `fail_count`
    calls, then returns the canned payload thereafter.
    """

    def __init__(
        self,
        fail_count: int = 0,
        payload: list[list[float]] | None = None,
    ) -> None:
        self._fail_count = fail_count
        self._payload: list[list[float]] = (
            payload if payload is not None else [[1000, 100.0, 101.0, 99.0, 100.5, 1000.0]]
        )
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 200,
    ) -> list[list[float]]:
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit},
        )
        self.call_count += 1
        if self.call_count <= self._fail_count:
            raise ccxt.NetworkError(f"simulated transient failure {self.call_count}")
        return list(self._payload)


def test_fetch_recent_ohlcv_returns_client_payload_on_first_try(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("marketmind_workers.trader.exchanges.time.sleep", lambda _t: None)
    payload = [[1000, 100.0, 101.0, 99.0, 100.5, 1000.0]]
    client = _FlakyClient(fail_count=0, payload=payload)
    adapter = BinanceAdapter(client=client)

    result = adapter.fetch_recent_ohlcv("BTC/USDT", "4h", limit=200)

    assert result == payload
    assert client.call_count == 1
    assert client.calls[0] == {
        "symbol": "BTC/USDT",
        "timeframe": "4h",
        "since": None,
        "limit": 200,
    }


def test_fetch_recent_ohlcv_retries_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two transient failures, success on the third. Verifies the
    retry count AND the exponential backoff schedule (1s, 2s).
    """
    sleep_calls: list[float] = []
    monkeypatch.setattr("marketmind_workers.trader.exchanges.time.sleep", sleep_calls.append)

    client = _FlakyClient(fail_count=2)
    adapter = BinanceAdapter(client=client)
    result = adapter.fetch_recent_ohlcv("BTC/USDT", "4h")

    assert client.call_count == 3
    assert len(result) == 1
    # Two sleeps between three attempts. Exponential backoff: 1s, 2s.
    assert sleep_calls == [1.0, 2.0]


def test_fetch_recent_ohlcv_raises_ingestion_error_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("marketmind_workers.trader.exchanges.time.sleep", lambda _t: None)

    client = _FlakyClient(fail_count=100)  # always fails
    adapter = BinanceAdapter(client=client)
    with pytest.raises(IngestionError, match="after 3 attempts"):
        adapter.fetch_recent_ohlcv("BTC/USDT", "4h")
    # Exactly _MAX_RETRIES attempts, no more.
    assert client.call_count == 3


def test_fetch_recent_ohlcv_retries_on_exchange_error_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ccxt.ExchangeError (e.g., a 5xx) is also a transient class."""
    monkeypatch.setattr("marketmind_workers.trader.exchanges.time.sleep", lambda _t: None)

    class _ExchangeFlaky(_FlakyClient):
        def fetch_ohlcv(
            self,
            symbol: str,
            timeframe: str,
            since: int | None = None,
            limit: int = 200,
        ) -> list[list[float]]:
            self.call_count += 1
            if self.call_count <= self._fail_count:
                raise ccxt.ExchangeError("simulated 503")
            return list(self._payload)

    client = _ExchangeFlaky(fail_count=1)
    adapter = BinanceAdapter(client=client)
    result = adapter.fetch_recent_ohlcv("BTC/USDT", "4h")

    assert client.call_count == 2
    assert len(result) == 1


def test_fetch_ohlcv_since_propagates_since_and_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("marketmind_workers.trader.exchanges.time.sleep", lambda _t: None)
    client = _FlakyClient(fail_count=0)
    adapter = BinanceAdapter(client=client)

    adapter.fetch_ohlcv_since("BTC/USDT", "4h", since_ms=12345, limit=500)

    assert client.calls == [
        {"symbol": "BTC/USDT", "timeframe": "4h", "since": 12345, "limit": 500},
    ]


def test_adapter_does_not_construct_default_client_when_one_passed() -> None:
    """Sanity gate against accidental network IO during tests: when
    a fake client is passed in, the adapter must NOT call
    `_make_binance_client`. Verified by passing a sentinel object
    and confirming `_client` is that sentinel.
    """
    sentinel = _FlakyClient()
    adapter = BinanceAdapter(client=sentinel)
    # `_client` is "private" but accessing it for this test is the
    # cleanest way to confirm no fallback to the default factory.
    assert adapter._client is sentinel
