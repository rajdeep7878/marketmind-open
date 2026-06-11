"""Unit tests for the Binance OHLCV market-data service.

ccxt is mocked entirely — these tests don't touch real Binance. One
opt-in @pytest.mark.integration test hits the real exchange for a
10-bar BTC/USDT daily slice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt
import pytest
from marketmind_workers.services import market_data
from marketmind_workers.services.market_data import (
    NetworkError,
    NoDataError,
    UnsupportedSymbolError,
    UnsupportedTimeframeError,
    fetch_ohlcv,
    get_market_data,
)

# ---- Helpers ---------------------------------------------------------------


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _bar(t_ms: int, *, base: float = 100.0) -> list[float]:
    return [t_ms, base, base + 1, base - 1, base + 0.5, 1000.0]


class _FakeBinance:
    """Stand-in for ccxt.binance.

    Holds an in-memory dict of {(symbol, timeframe): list_of_bars}.
    fetch_ohlcv() paginates over the bars where t >= since, returning
    up to `limit` bars per call.
    """

    def __init__(self, bars_by_key: dict[tuple[str, str], list[list[float]]]) -> None:
        self.bars_by_key = bars_by_key
        self.calls: list[dict[str, Any]] = []

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        limit: int,
    ) -> list[list[float]]:
        self.calls.append(
            {"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit}
        )
        all_bars = self.bars_by_key.get((symbol, timeframe), [])
        return [b for b in all_bars if b[0] >= since][:limit]


def _make_synthetic_bars(
    symbol: str,
    timeframe: str,
    n: int,
    start: datetime,
    bar_ms: int,
) -> list[list[float]]:
    start_ms = _ms(start)
    return [_bar(start_ms + i * bar_ms, base=100.0 + i) for i in range(n)]


# ---- Symbol / timeframe / datetime validation ------------------------------


def test_get_market_data_rejects_unsupported_symbol() -> None:
    with pytest.raises(UnsupportedSymbolError):
        get_market_data(
            "FAKE/USDT",
            "1d",
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
        )


def test_get_market_data_rejects_unsupported_timeframe() -> None:
    with pytest.raises(UnsupportedTimeframeError):
        get_market_data(
            "BTC/USDT",
            "2h",  # not in the v1.0 set
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
        )


def test_get_market_data_rejects_naive_datetime() -> None:
    with pytest.raises(market_data.MarketDataError, match="timezone-aware UTC"):
        get_market_data(
            "BTC/USDT",
            "1d",
            datetime(2024, 1, 1),  # noqa: DTZ001  # naive is the point
            datetime(2024, 1, 2, tzinfo=UTC),
        )


def test_get_market_data_rejects_end_before_start() -> None:
    with pytest.raises(market_data.MarketDataError, match="must be strictly after"):
        get_market_data(
            "BTC/USDT",
            "1d",
            datetime(2024, 1, 5, tzinfo=UTC),
            datetime(2024, 1, 1, tzinfo=UTC),
        )


def test_majors_in_whitelist() -> None:
    for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"):
        assert sym in market_data.SUPPORTED_SYMBOLS


# ---- fetch_ohlcv (mocked ccxt) ---------------------------------------------


def test_fetch_ohlcv_happy_single_page() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=10)
    bar_ms = 24 * 60 * 60 * 1000
    bars = _make_synthetic_bars("BTC/USDT", "1d", 10, start, bar_ms)
    sdk = _FakeBinance({("BTC/USDT", "1d"): bars})

    df = fetch_ohlcv("BTC/USDT", "1d", start, end, client=sdk)
    assert len(df) == 10
    assert df.index.tz is not None
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    # First bar's open time matches `start`
    assert df.index[0] == start


def test_fetch_ohlcv_paginates_when_over_limit() -> None:
    # Force >1000 bars: 1500 minutes at 1m
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bar_ms = 60_000
    n = 1500
    bars = _make_synthetic_bars("BTC/USDT", "1m", n, start, bar_ms)
    end = start + timedelta(minutes=n)
    sdk = _FakeBinance({("BTC/USDT", "1m"): bars})

    df = fetch_ohlcv("BTC/USDT", "1m", start, end, client=sdk)
    assert len(df) == n
    # Should have paginated: 1500 / 1000 = 2 pages
    assert len(sdk.calls) >= 2


def test_fetch_ohlcv_no_data_raises() -> None:
    sdk = _FakeBinance({})
    with pytest.raises(NoDataError):
        fetch_ohlcv(
            "BTC/USDT",
            "1d",
            datetime(2030, 1, 1, tzinfo=UTC),
            datetime(2030, 1, 5, tzinfo=UTC),
            client=sdk,
        )


def test_fetch_ohlcv_translates_network_error() -> None:
    class _BoomBinance:
        def fetch_ohlcv(self, *_a: Any, **_kw: Any) -> Any:
            raise ccxt.NetworkError("dns timeout")

    with pytest.raises(NetworkError, match="network error"):
        fetch_ohlcv(
            "BTC/USDT",
            "1d",
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
            client=_BoomBinance(),
        )


def test_fetch_ohlcv_translates_exchange_error() -> None:
    class _BoomBinance:
        def fetch_ohlcv(self, *_a: Any, **_kw: Any) -> Any:
            raise ccxt.ExchangeError("symbol not listed")

    with pytest.raises(NetworkError, match="exchange error"):
        fetch_ohlcv(
            "BTC/USDT",
            "1d",
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
            client=_BoomBinance(),
        )


# ---- get_market_data — caching ---------------------------------------------


def test_get_market_data_cold_cache_writes_parquet(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=5)
    bar_ms = 24 * 60 * 60 * 1000
    bars = _make_synthetic_bars("BTC/USDT", "1d", 5, start, bar_ms)
    sdk = _FakeBinance({("BTC/USDT", "1d"): bars})

    df = get_market_data("BTC/USDT", "1d", start, end, data_dir=tmp_path, client=sdk)
    assert len(df) == 5

    parquet = tmp_path / "cache" / "market" / "BTC_USDT" / "1d.parquet"
    assert parquet.exists()


def test_get_market_data_warm_cache_skips_network(tmp_path: Path) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=5)
    bar_ms = 24 * 60 * 60 * 1000
    bars = _make_synthetic_bars("BTC/USDT", "1d", 5, start, bar_ms)
    sdk = _FakeBinance({("BTC/USDT", "1d"): bars})

    # Populate
    get_market_data("BTC/USDT", "1d", start, end, data_dir=tmp_path, client=sdk)
    fresh_calls = len(sdk.calls)

    # Read back over the same range — no new fetches expected
    df = get_market_data("BTC/USDT", "1d", start, end, data_dir=tmp_path, client=sdk)
    assert len(df) == 5
    assert len(sdk.calls) == fresh_calls  # unchanged


def test_get_market_data_partial_cache_fills_back_gap(tmp_path: Path) -> None:
    """Cache covers first 5 days; caller asks for first 8 days. We
    should fetch only days 6-8 and merge."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    bar_ms = 24 * 60 * 60 * 1000
    full_bars = _make_synthetic_bars("BTC/USDT", "1d", 10, start, bar_ms)
    sdk = _FakeBinance({("BTC/USDT", "1d"): full_bars})

    # First call: 5 days
    first_end = start + timedelta(days=5)
    get_market_data("BTC/USDT", "1d", start, first_end, data_dir=tmp_path, client=sdk)
    calls_after_first = len(sdk.calls)

    # Second call: 8 days — gap is days 5..8
    second_end = start + timedelta(days=8)
    df = get_market_data("BTC/USDT", "1d", start, second_end, data_dir=tmp_path, client=sdk)
    assert len(df) == 8
    # A new fetch must have happened to cover days 5..8
    assert len(sdk.calls) > calls_after_first
    # And the very last call's `since` must start at day 5, not day 0
    last_since_ms = sdk.calls[-1]["since"]
    expected_since_ms = _ms(start + timedelta(days=5))
    assert last_since_ms == expected_since_ms


def test_get_market_data_partial_cache_fills_front_gap(tmp_path: Path) -> None:
    """Cache covers days 5-10; caller asks for days 0-10. Front gap
    fetched, then merged with the cache."""
    start_orig = datetime(2024, 1, 1, tzinfo=UTC)
    bar_ms = 24 * 60 * 60 * 1000
    all_bars = _make_synthetic_bars("BTC/USDT", "1d", 10, start_orig, bar_ms)
    sdk = _FakeBinance({("BTC/USDT", "1d"): all_bars})

    # Seed cache with the back half
    get_market_data(
        "BTC/USDT",
        "1d",
        start_orig + timedelta(days=5),
        start_orig + timedelta(days=10),
        data_dir=tmp_path,
        client=sdk,
    )
    # Request full range; only days 0..5 should be fetched fresh
    df = get_market_data(
        "BTC/USDT",
        "1d",
        start_orig,
        start_orig + timedelta(days=10),
        data_dir=tmp_path,
        client=sdk,
    )
    assert len(df) == 10
    assert df.index[0] == start_orig


# ---- Symbol/dirname round-trip -------------------------------------------


def test_symbol_to_dirname_filesystem_safe() -> None:
    assert market_data._symbol_to_dirname("BTC/USDT") == "BTC_USDT"
    assert market_data._symbol_to_dirname("1INCH/USDT") == "1INCH_USDT"


# ---- Phase C C.7: FX symbol admission (cached-only) ----------------------


def test_eurusd_is_whitelisted_for_validation() -> None:
    """C.7: EUR/USD admitted by the symbol whitelist so engine + benchmark
    + monte-carlo can ask for it. The widening is the minimum-path fix
    for the crypto-only data path; production FX cache is operator-
    populated, not Binance-fetched. The proper multi-adapter market-
    data service routing on asset_class is deferred to a later sub-phase.
    """
    from marketmind_workers.services.market_data import SUPPORTED_SYMBOLS

    assert "EUR/USD" in SUPPORTED_SYMBOLS, (
        "Phase C.7 added EUR/USD to the whitelist so the cached-only "
        "FX backtest path admits it. If this fails the C.7 widening "
        "was reverted; re-check `services/market_data.py`."
    )


def test_eurusd_cached_path_served_without_binance(tmp_path: Path) -> None:
    """When a parquet cache file exists for EUR/USD, `get_market_data`
    serves it from disk without invoking the Binance fetcher. This is
    the operational pattern for C.7: pre-populate the cache via the
    FX fixture or an Oanda dump; backtest reads from disk.

    Pattern: seed the cache via one call with a generous fake SDK,
    then re-read with a raising SDK to prove cache-hit.
    """
    bar_ms = 60 * 60 * 1000  # 1H
    start_orig = datetime(2025, 6, 2, tzinfo=UTC)  # a Monday
    seed_bars = _make_synthetic_bars("EUR/USD", "1h", 24, start_orig, bar_ms)
    seed_sdk = _FakeBinance({("EUR/USD", "1h"): seed_bars})

    # Seed: full 24-hour day. Cache file lands at
    # tmp_path/cache/market/EUR_USD/1h.parquet.
    get_market_data(
        "EUR/USD",
        "1h",
        start_orig,
        start_orig + timedelta(hours=24),
        data_dir=tmp_path,
        client=seed_sdk,
    )

    # Re-read a subrange: must serve from cache. The raising SDK proves
    # `fetch_ohlcv` was not invoked.
    class _RaisingSDK:
        def fetch_ohlcv(self, *args: Any, **kwargs: Any) -> list[list[Any]]:
            raise AssertionError(
                "FX `fetch_ohlcv` must NOT be called when cache covers the requested range",
            )

    df = get_market_data(
        "EUR/USD",
        "1h",
        start_orig + timedelta(hours=2),
        start_orig + timedelta(hours=7),
        data_dir=tmp_path,
        client=_RaisingSDK(),
    )
    assert len(df) == 5  # bars at hours 2, 3, 4, 5, 6 (end exclusive)
    assert df.index[0] == start_orig + timedelta(hours=2)


# ---- Integration: real Binance (opt-in) ----------------------------------


@pytest.mark.integration
def test_get_market_data_real_binance(tmp_path: Path) -> None:
    """Real ccxt call against Binance for 10 daily candles. Excluded
    from CI; runnable locally with `uv run pytest -m integration`."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=10)
    df = get_market_data("BTC/USDT", "1d", start, end, data_dir=tmp_path)
    assert 8 <= len(df) <= 10  # tolerance for weekend/maintenance gaps
    assert df.index.tz is not None
    assert set(df.columns) == {"open", "high", "low", "close", "volume"}
    # Sanity: BTC was above $20k throughout Jan 2024.
    assert (df["close"] > 20_000).all()
