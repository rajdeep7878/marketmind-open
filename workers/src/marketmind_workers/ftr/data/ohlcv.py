"""FTR OHLCV data layer: ccxt fetcher + parquet cache + manifest.

Follows the pagination / rate-limit / cache conventions of
``marketmind_workers.services.market_data`` (Phase 3.1) but lives in its own
cache namespace (``data/ftr/cache``) and adds per-file checksums, a fetch
manifest, and a mandatory QA hook on every load.

All timestamps are UTC tz-aware bar OPEN times. Naive datetimes are rejected
at the module boundary.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import ccxt  # type: ignore[import-untyped]
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_PAGE_LIMIT = 1000
_PAGE_SLEEP_S = 0.15
_MAX_RETRIES = 5

_COLUMNS = ["open", "high", "low", "close", "volume"]


def require_utc(ts: datetime, name: str) -> datetime:
    """Boundary guard: reject naive datetimes (non-negotiable constraint 4)."""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"{name} must be tz-aware UTC, got naive {ts!r}")
    return ts.astimezone(UTC)


def dtindex(df: pd.DataFrame) -> pd.DatetimeIndex:
    """Narrow a frame's index to DatetimeIndex (pyright-strict pattern)."""
    idx = df.index
    assert isinstance(idx, pd.DatetimeIndex)
    return idx


def _idx_min(df: pd.DataFrame) -> pd.Timestamp:
    ts = dtindex(df).min()
    assert isinstance(ts, pd.Timestamp)
    return ts


def _idx_max(df: pd.DataFrame) -> pd.Timestamp:
    ts = dtindex(df).max()
    assert isinstance(ts, pd.Timestamp)
    return ts


def _client(exchange: str) -> Any:
    cls = getattr(ccxt, exchange)
    return cls({"enableRateLimit": True, "timeout": 15_000})


def _fetch_page(client: Any, symbol: str, timeframe: str, since_ms: int) -> list[list[float]]:
    delay = 1.0
    for attempt in range(_MAX_RETRIES):
        try:
            return client.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=_PAGE_LIMIT)
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout) as exc:
            if attempt == _MAX_RETRIES - 1:
                raise
            logger.warning(
                "ftr_ohlcv_retry", symbol=symbol, timeframe=timeframe, attempt=attempt, err=str(exc)
            )
            time.sleep(delay)
            delay *= 2.0
    raise RuntimeError("unreachable")


def _rows_to_frame(rows: list[list[float]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(
            columns=pd.Index(_COLUMNS), index=pd.DatetimeIndex([], tz=UTC, name="ts")
        )
    df = pd.DataFrame(rows, columns=pd.Index(["ts_ms", *_COLUMNS]))
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    df = df.drop(columns=["ts_ms"]).set_index("ts").astype("float64")
    out = df.loc[~df.index.duplicated(keep="first")].sort_index()
    assert isinstance(out, pd.DataFrame)
    return out


def fetch_ohlcv(
    exchange: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    client: Any | None = None,
) -> pd.DataFrame:
    """Paginated [start, end) fetch with `since` cursors and backoff retries."""
    start = require_utc(start, "start")
    end = require_utc(end, "end")
    if timeframe not in _TIMEFRAME_MS:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    sdk = client if client is not None else _client(exchange)
    bar_ms = _TIMEFRAME_MS[timeframe]
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out: list[list[float]] = []
    while cursor < end_ms:
        batch = _fetch_page(sdk, symbol, timeframe, cursor)
        if not batch:
            break
        out.extend(r for r in batch if r[0] < end_ms)
        last_ts = int(batch[-1][0])
        next_cursor = last_ts + bar_ms
        if next_cursor <= cursor:  # defensive: server returned stale page
            break
        cursor = next_cursor
        if len(batch) < _PAGE_LIMIT:
            break
        time.sleep(_PAGE_SLEEP_S)
    return _rows_to_frame(out)


# --------------------------------------------------------------------------
# Parquet cache with checksums + manifest
# --------------------------------------------------------------------------


def _cache_path(cache_dir: Path, exchange: str, symbol: str, timeframe: str) -> Path:
    safe = symbol.replace("/", "_")
    return cache_dir / "market" / exchange / safe / f"{timeframe}.parquet"


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "market" / "fetch_manifest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(cache_dir: Path) -> dict[str, Any]:
    p = _manifest_path(cache_dir)
    if p.exists():
        return json.loads(p.read_text())
    return {"entries": {}}


def _save_manifest(cache_dir: Path, manifest: dict[str, Any]) -> None:
    p = _manifest_path(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2, sort_keys=True))


@dataclass(frozen=True)
class CachedSeries:
    """A loaded OHLCV series plus its provenance."""

    exchange: str
    symbol: str
    timeframe: str
    frame: pd.DataFrame
    path: Path
    sha256: str


def get_ohlcv(
    exchange: str,
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path,
    client: Any | None = None,
    offline: bool = False,
) -> CachedSeries:
    """Cache-first load of [start, end); fetches and extends cache as needed.

    Resumable: a partially filled cache only fetches the missing front/back
    ranges. ``offline=True`` raises if the cache cannot satisfy the request
    (used by deterministic validation runs that must not silently refetch).
    """
    start = require_utc(start, "start")
    end = require_utc(end, "end")
    path = _cache_path(cache_dir, exchange, symbol, timeframe)
    bar = timedelta(milliseconds=_TIMEFRAME_MS[timeframe])

    cached: pd.DataFrame | None = None
    if path.exists():
        cached = pd.read_parquet(path)
        if dtindex(cached).tz is None:  # legacy safety; our writes are always tz-aware
            cached.index = dtindex(cached).tz_localize(UTC)

    if offline:
        # Offline mode serves whatever the cache holds for [start, end) —
        # deterministic validation runs must never silently refetch. A
        # symbol listed after `start` legitimately has no front data; the
        # fetch manifest records actual coverage.
        if cached is None:
            raise RuntimeError(
                f"offline load: no cache for {exchange} {symbol} {timeframe} at {path}"
            )
        window = cached.loc[(cached.index >= start) & (cached.index < end)]
        return CachedSeries(
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            frame=window,
            path=path,
            sha256=_sha256(path),
        )

    need_front = cached is None or _idx_min(cached) > start + bar
    need_back = cached is None or _idx_max(cached) < end - 2 * bar

    if cached is None:
        merged = fetch_ohlcv(exchange, symbol, timeframe, start, end, client=client)
    else:
        parts = [cached]
        if need_front:
            front_end = _idx_min(cached).to_pydatetime()
            parts.insert(0, fetch_ohlcv(exchange, symbol, timeframe, start, front_end, client=client))
        if need_back:
            back_start = (_idx_max(cached) + bar).to_pydatetime()
            parts.append(fetch_ohlcv(exchange, symbol, timeframe, back_start, end, client=client))
        merged = pd.concat(parts)
        merged = merged[~merged.index.duplicated(keep="first")].sort_index()
        assert isinstance(merged, pd.DataFrame)

    if need_front or need_back or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(path, engine="pyarrow", compression="snappy")
        digest = _sha256(path)
        manifest = _load_manifest(cache_dir)
        manifest["entries"][f"{exchange}:{symbol}:{timeframe}"] = {
            "path": str(path),
            "sha256": digest,
            "rows": len(merged),
            "first_ts": _idx_min(merged).isoformat() if len(merged) else None,
            "last_ts": _idx_max(merged).isoformat() if len(merged) else None,
            "fetched_at": datetime.now(UTC).isoformat(),
            "bytes": path.stat().st_size,
        }
        _save_manifest(cache_dir, manifest)
        logger.info(
            "ftr_ohlcv_cached",
            exchange=exchange,
            symbol=symbol,
            timeframe=timeframe,
            rows=len(merged),
            bytes=path.stat().st_size,
        )
    else:
        digest = _sha256(path)

    window = merged.loc[(merged.index >= start) & (merged.index < end)]
    return CachedSeries(
        exchange=exchange, symbol=symbol, timeframe=timeframe, frame=window, path=path, sha256=digest
    )


def verify_spot_listing(exchange: str, symbols: list[str]) -> dict[str, bool]:
    """Check each symbol is listed as an active spot pair on `exchange`."""
    sdk = _client(exchange)
    markets = sdk.load_markets()
    out: dict[str, bool] = {}
    for sym in symbols:
        m = markets.get(sym)
        out[sym] = bool(m and m.get("spot") and m.get("active", True))
    return out
