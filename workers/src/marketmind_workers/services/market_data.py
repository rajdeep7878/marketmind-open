"""Historical Binance OHLCV via ccxt + Parquet on-disk cache.

`get_market_data(symbol, timeframe, start, end)` is the public entry
point. It returns a clean pandas DataFrame:

  index: UTC-aware DatetimeIndex (each row = the bar's OPEN time)
  columns: open, high, low, close, volume   (float64)

Cache layout:
  ${data_dir}/cache/market/{symbol_sanitized}/{timeframe}.parquet
  e.g. /data/cache/market/BTC_USDT/4h.parquet

Sanitization just replaces "/" with "_" so the symbol is filesystem-
safe.  The Parquet file always holds a contiguous range; on a partial
miss we fetch ONLY the missing span and merge.

Whitelist: 50 of Binance's highest-volume USDT spot pairs. We don't
let extracted specs request arbitrary symbols — the Phase 1 spec
restricts instruments to crypto spot pairs, and the whitelist gives
us a small, well-supported set that maps cleanly to ccxt's
`fetch_ohlcv` calls.
"""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import ccxt
import pandas as pd
import structlog

log = structlog.get_logger(__name__)


# ---- Errors ----------------------------------------------------------------


class MarketDataError(Exception):
    """Base for any failure raised by the market-data service."""


class UnsupportedSymbolError(MarketDataError):
    """Symbol is not in the v1.0 whitelist."""


class UnsupportedTimeframeError(MarketDataError):
    """Timeframe is outside the Phase 1 fixed set."""


class NoDataError(MarketDataError):
    """Binance returned no candles for the requested range (e.g. a coin
    that didn't exist yet, or a future date range).
    """


class NetworkError(MarketDataError):
    """ccxt raised an exchange/network error during fetch."""


# ---- Whitelist + supported timeframes -------------------------------------


# Top 50 Binance USDT-quoted spot pairs by long-run volume. Hand-curated;
# extracted strategies that target a pair outside this set are rejected
# at the data-fetch boundary. Phase 4 can expand this from Binance's
# exchange-info endpoint if we need more coverage.
SUPPORTED_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        # majors
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "BNB/USDT",
        "XRP/USDT",
        # large-caps
        "ADA/USDT",
        "DOGE/USDT",
        "AVAX/USDT",
        "DOT/USDT",
        "TRX/USDT",
        "LINK/USDT",
        "MATIC/USDT",
        "LTC/USDT",
        "BCH/USDT",
        "ATOM/USDT",
        # mid-caps + DeFi heavy hitters
        "UNI/USDT",
        "AAVE/USDT",
        "MKR/USDT",
        "COMP/USDT",
        "SUSHI/USDT",
        "CRV/USDT",
        "YFI/USDT",
        "SNX/USDT",
        "1INCH/USDT",
        "REN/USDT",
        # ecosystem coins
        "FTM/USDT",
        "NEAR/USDT",
        "ALGO/USDT",
        "ICP/USDT",
        "VET/USDT",
        "FIL/USDT",
        "EOS/USDT",
        "XTZ/USDT",
        "EGLD/USDT",
        "ONE/USDT",
        # newer + layer 2s + popular alts
        "ARB/USDT",
        "OP/USDT",
        "APT/USDT",
        "SUI/USDT",
        "TIA/USDT",
        "SEI/USDT",
        "INJ/USDT",
        # storage / oracle / privacy
        "STX/USDT",
        "GRT/USDT",
        "RUNE/USDT",
        "XLM/USDT",
        "XMR/USDT",
        "DASH/USDT",
        # memes that have stuck around
        "SHIB/USDT",
        "PEPE/USDT",
        "WIF/USDT",
        # ----- Phase C C.7: FX majors (cached-only) ---------------------
        # The production data fetcher in this module (`fetch_ohlcv` via
        # ccxt + Binance) targets crypto. FX symbols are admitted by
        # `_validate_symbol` for backtest + benchmark + monte-carlo
        # consumption from the on-disk parquet cache only. If
        # `get_market_data` would need to backfill an FX symbol it falls
        # through to `fetch_ohlcv` which will raise a clear ccxt error
        # ("Invalid symbol" — Binance has no EUR/USD pair). Operators
        # pre-populate `/data/cache/market/EUR_USD/<tf>.parquet` from a
        # historical Oanda dump (see `scripts/fetch_eurusd_2025_fixture.py`
        # or the perf-regression fixture at
        # `tests/fixtures/market/eurusd_1h_2025.parquet`). The proper
        # multi-adapter market-data service routing on asset_class is
        # deferred to a future Phase C sub-phase; for C.7's first FX
        # seed the cached-only path is sufficient.
        "EUR/USD",
        # "EURUSD" alias surfaced by Hunt 16's extraction (2026-05-26):
        # the LLM produced the no-slash FX-trading shorthand instead of
        # the codebase-canonical "EUR/USD" with slash. Both forms admit;
        # operators populate the corresponding cache dir (EUR_USD vs
        # EURUSD). Long-term, extraction-prompt teaching should canonicalise
        # to the with-slash form to match the BTC/USDT convention in the
        # existing crypto strategies.
        "EURUSD",
    },
)


# Mirror of Phase 1's Timeframe enum. Listed here as strings so the
# service is independent of the shared package's enum representation;
# callers can pass either the enum member's value or the bare string.
SUPPORTED_TIMEFRAMES: Final[frozenset[str]] = frozenset(
    {"1m", "5m", "15m", "30m", "1h", "4h", "1d"},
)


# ms-per-bar for each timeframe. Used both for pagination math (the
# `since` parameter advances by `limit * bar_ms`) and for sanity checks
# on the returned data spacing.
_TIMEFRAME_MS: Final[dict[str, int]] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


# ccxt's fetch_ohlcv caps at ~1000 candles per call for Binance. We
# leave a bit of headroom so partial responses don't bite the
# pagination loop.
_FETCH_LIMIT: Final[int] = 1000


# Small inter-page sleep on top of ccxt's enableRateLimit. The Binance
# spot endpoint allows 1200 weight per minute; one OHLCV call costs
# 1 weight, so we have headroom — but a tiny sleep keeps us defensive.
_PAGE_SLEEP_S: Final[float] = 0.1


# Filesystem-safe symbol form: "BTC/USDT" -> "BTC_USDT".
_SYMBOL_FS_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Z0-9]+")


def _symbol_to_dirname(symbol: str) -> str:
    return _SYMBOL_FS_RE.sub("_", symbol.upper())


# ---- Validation helpers ----------------------------------------------------


def _validate_symbol(symbol: str) -> None:
    if symbol not in SUPPORTED_SYMBOLS:
        raise UnsupportedSymbolError(
            f"symbol {symbol!r} is not in the supported set "
            f"({len(SUPPORTED_SYMBOLS)} Binance spot pairs). "
            f"See SUPPORTED_SYMBOLS for the current list.",
        )


def _validate_timeframe(timeframe: str) -> None:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise UnsupportedTimeframeError(
            f"timeframe {timeframe!r} is not supported. Allowed: {sorted(SUPPORTED_TIMEFRAMES)}",
        )


def _ensure_utc(dt: datetime, *, name: str) -> datetime:
    """Reject naive datetimes and non-UTC tz-aware ones."""
    if dt.tzinfo is None:
        raise MarketDataError(f"{name} must be timezone-aware UTC; got naive datetime")
    offset = dt.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise MarketDataError(f"{name} must be UTC (offset 0); got offset {offset}")
    return dt


# ---- ccxt client factory ---------------------------------------------------


def _make_binance_client() -> ccxt.binance:
    """Build a Binance spot client. Pulled out so tests can mock it.

    enableRateLimit=True is required: ccxt will throttle calls internally
    to stay under Binance's per-minute weight ceiling.
    """
    return ccxt.binance({"enableRateLimit": True})


# ---- Fetch (paginated, uncached) ------------------------------------------


def _ms(dt: datetime) -> int:
    """UTC datetime to milliseconds since epoch — ccxt's `since` unit."""
    return int(dt.timestamp() * 1000)


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    client: Any | None = None,
) -> pd.DataFrame:
    """Fetch the OHLCV range [start, end] from Binance via ccxt.

    Paginated: ccxt's fetch_ohlcv returns up to ~1000 candles per call,
    so we walk forward in batches keyed on `since`. The returned
    DataFrame is indexed by UTC-aware DatetimeIndex of bar OPEN times
    and trimmed to [start, end] inclusive of bars whose open >= start
    and < end (half-open interval on the upper edge — typical OHLCV
    behaviour because the "end" bar isn't closed yet).
    """
    _validate_symbol(symbol)
    _validate_timeframe(timeframe)
    start = _ensure_utc(start, name="start")
    end = _ensure_utc(end, name="end")
    if end <= start:
        raise MarketDataError(f"end ({end}) must be strictly after start ({start})")

    sdk = client if client is not None else _make_binance_client()
    bar_ms = _TIMEFRAME_MS[timeframe]
    start_ms = _ms(start)
    end_ms = _ms(end)

    rows: list[list[float]] = []
    cursor_ms = start_ms
    page = 0
    while cursor_ms < end_ms:
        page += 1
        try:
            batch = sdk.fetch_ohlcv(symbol, timeframe, since=cursor_ms, limit=_FETCH_LIMIT)
        except ccxt.NetworkError as exc:
            raise NetworkError(f"network error fetching {symbol} {timeframe}: {exc}") from exc
        except ccxt.ExchangeError as exc:
            raise NetworkError(f"exchange error fetching {symbol} {timeframe}: {exc}") from exc
        if not batch:
            # Binance returns an empty list when the cursor is past the
            # most recent bar OR when the symbol has no data yet.
            break

        # Trim batch to the [start, end) window so we don't accidentally
        # include candles outside what the caller asked for.
        filtered = [row for row in batch if start_ms <= row[0] < end_ms]
        rows.extend(filtered)

        last_ms = batch[-1][0]
        # Advance the cursor; +bar_ms so the next call's `since` doesn't
        # re-fetch the last bar of the previous batch.
        next_cursor = last_ms + bar_ms
        if next_cursor <= cursor_ms:
            # Defensive: shouldn't happen, but guards against an infinite loop
            # if Binance returns a stale batch.
            break
        cursor_ms = next_cursor

        # If the batch was shorter than the limit we've reached the
        # end of available data; no point hammering for more.
        if len(batch) < _FETCH_LIMIT:
            break

        time.sleep(_PAGE_SLEEP_S)

    if not rows:
        raise NoDataError(
            f"Binance returned no candles for {symbol} {timeframe} "
            f"in [{start.isoformat()}, {end.isoformat()})",
        )

    df = pd.DataFrame(
        rows,
        columns=pd.Index(["timestamp_ms", "open", "high", "low", "close", "volume"]),
    )
    df["timestamp"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    df = df.drop(columns=["timestamp_ms"]).set_index("timestamp")
    # Deduplicate just in case pagination overlapped; keep the first.
    deduped = df[~df.index.duplicated(keep="first")].sort_index()
    assert isinstance(deduped, pd.DataFrame)
    df = deduped
    log.info(
        "market_data_fetched",
        symbol=symbol,
        timeframe=timeframe,
        rows=len(df),
        pages=page,
        start=start.isoformat(),
        end=end.isoformat(),
    )
    return df


# ---- Cached wrapper --------------------------------------------------------


def _cache_path(data_dir: str | Path, symbol: str, timeframe: str) -> Path:
    return Path(data_dir) / "cache" / "market" / _symbol_to_dirname(symbol) / f"{timeframe}.parquet"


def _read_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        log.warning("market_data_cache_corrupt", path=str(path), error=str(exc))
        return None
    # pd.read_parquet's static type is Series | DataFrame; in practice
    # an OHLCV cache file always round-trips as a DataFrame.
    assert isinstance(df, pd.DataFrame)
    idx = df.index
    assert isinstance(idx, pd.DatetimeIndex)
    # Be defensive: ensure index is tz-aware UTC. Older writes might
    # have lost tz on round-trip if pyarrow's options changed.
    if idx.tz is None:
        df.index = idx.tz_localize(UTC)
    return df


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, engine="pyarrow", compression="snappy")


def _slice(df: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame:
    """Half-open slice [start, end). Mirrors fetch_ohlcv's window."""
    return df.loc[(df.index >= start) & (df.index < end)]


def get_market_data(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    data_dir: str | Path = "/data",
    client: Any | None = None,
) -> pd.DataFrame:
    """Cached OHLCV fetch. Disk cache is the source of truth; missing
    spans are fetched from Binance and merged.

    Returns a DataFrame in the requested [start, end) window, indexed
    by tz-aware UTC bar-open timestamps with float64 OHLCV columns.
    """
    _validate_symbol(symbol)
    _validate_timeframe(timeframe)
    start = _ensure_utc(start, name="start")
    end = _ensure_utc(end, name="end")
    if end <= start:
        raise MarketDataError(f"end ({end}) must be strictly after start ({start})")

    cache_file = _cache_path(data_dir, symbol, timeframe)
    cached = _read_cache(cache_file)

    if cached is None or cached.empty:
        # Cold cache: fetch the whole range.
        df = fetch_ohlcv(symbol, timeframe, start, end, client=client)
        _write_cache(cache_file, df)
        return _slice(df, start, end)

    cached_idx = cached.index
    assert isinstance(cached_idx, pd.DatetimeIndex)
    # Index slots on a non-empty DatetimeIndex are real Timestamps at
    # runtime. The pandas-stubs union type doesn't narrow well; an
    # explicit Timestamp construction sidesteps that.
    first_ts = pd.Timestamp(cached_idx[0])  # type: ignore[arg-type]
    last_ts = pd.Timestamp(cached_idx[-1])  # type: ignore[arg-type]
    cached_start: datetime = first_ts.to_pydatetime()
    cached_end: datetime = last_ts.to_pydatetime()
    bar_ms = _TIMEFRAME_MS[timeframe]
    bar_seconds = bar_ms / 1000.0

    new_pieces: list[pd.DataFrame] = []

    # Missing span at the front (older than cache covers).
    if start < cached_start:
        # pyright narrows cached_start to datetime|NaTType because the
        # stubs for DatetimeIndex.[index] are unhelpfully broad. At
        # runtime we already proved (assert + Timestamp construction)
        # that cached_start is a real datetime.
        front = fetch_ohlcv(symbol, timeframe, start, cached_start, client=client)  # type: ignore[arg-type]
        new_pieces.append(front)

    # Missing span at the back. cached_end is the LAST bar in the cache;
    # we want to fetch from the NEXT bar onwards. pd.Timedelta is a
    # subclass of datetime.timedelta, so the sum here is already a
    # plain datetime — no .to_pydatetime() needed.
    next_after_cache = cached_end + pd.Timedelta(seconds=bar_seconds)
    if end > next_after_cache:
        back = fetch_ohlcv(symbol, timeframe, next_after_cache, end, client=client)  # type: ignore[arg-type]
        new_pieces.append(back)

    if new_pieces:
        merged: pd.DataFrame = pd.concat([cached, *new_pieces])
        deduped = merged[~merged.index.duplicated(keep="first")].sort_index()
        assert isinstance(deduped, pd.DataFrame)
        _write_cache(cache_file, deduped)
        cached = deduped

    out = _slice(cached, start, end)
    if out.empty:
        raise NoDataError(
            f"no cached or fetched candles intersect [{start.isoformat()}, {end.isoformat()})",
        )

    # Optional sanity check: spacing should match the timeframe. Don't
    # raise — Binance occasionally has gaps — just log if something
    # looks off.
    if len(out) >= 2:
        actual_ms = (out.index[1] - out.index[0]).total_seconds() * 1000
        expected_ms = float(bar_ms)
        if not math.isclose(actual_ms, expected_ms, rel_tol=0.01):
            log.warning(
                "market_data_unexpected_spacing",
                symbol=symbol,
                timeframe=timeframe,
                actual_ms=actual_ms,
                expected_ms=expected_ms,
            )
    return out


__all__ = [
    "SUPPORTED_SYMBOLS",
    "SUPPORTED_TIMEFRAMES",
    "MarketDataError",
    "NetworkError",
    "NoDataError",
    "UnsupportedSymbolError",
    "UnsupportedTimeframeError",
    "fetch_ohlcv",
    "get_market_data",
]
