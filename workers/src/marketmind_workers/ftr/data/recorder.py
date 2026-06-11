"""Binance spot L1/L2 recorder — standalone async service (opt-in).

Streams for each configured symbol (BTC/USDT; ETH/USDT behind a flag):
- ``<sym>@depth@100ms``  diff depth events, with REST snapshot resync and
  ``lastUpdateId``/``U``/``u`` sequence-gap detection per the Binance spec
- ``<sym>@aggTrade``     aggregated trades
- ``<sym>@bookTicker``   best bid/ask

Output: hourly-rotated parquet files under ``data/ftr/recordings`` (a
gitignored directory) plus a per-hour integrity manifest with event counts,
sequence gaps, resync count, and uptime %. Raw recordings are for personal
research use and are never committed or redistributed.

This service is market-data-only by construction: the websocket endpoints
are public and unauthenticated, and no API key is ever read. Historical spot
L2/L1 data is not freely downloadable in bulk, so this recorder collects
forward — microstructure verdicts require >= 28 recorded days at >= 95%
uptime (Stage 4 sample gate) and are `INSUFFICIENT_DATA` until then.

Run: ``python -m marketmind_workers.ftr.data.recorder`` (Docker service
``ftr-recorder``, compose profile ``ftr``).
"""

from __future__ import annotations

import asyncio
import json
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

_WS_BASE = "wss://stream.binance.com:9443/stream"
_REST_SNAPSHOT = "https://api.binance.com/api/v3/depth"
_SNAPSHOT_LIMIT = 1000
_ROTATE_S = 3600
_FLUSH_EVERY_EVENTS = 5000


def _stream_name(symbol: str) -> str:
    return symbol.replace("/", "").lower()


@dataclass
class _HourBuffer:
    """Accumulates one hour of events per (symbol, channel) for rotation."""

    depth: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    book_ticker: list[dict[str, Any]] = field(default_factory=list)
    sequence_gaps: int = 0
    resyncs: int = 0
    first_event_ms: int | None = None
    last_event_ms: int | None = None


class DepthSequencer:
    """Tracks Binance diff-depth sequence continuity for one symbol.

    Binance spot rule: after a REST snapshot with ``lastUpdateId = L``, the
    first applied diff event must satisfy ``U <= L+1 <= u``; each subsequent
    event must have ``U == prev_u + 1``. A violation means events were lost:
    flag a gap and request a fresh snapshot.
    """

    def __init__(self) -> None:
        self.last_update_id: int | None = None
        self.synced = False

    def apply_snapshot(self, last_update_id: int) -> None:
        self.last_update_id = last_update_id
        self.synced = False  # waits for the first bridging diff event

    def check(self, first_id: int, final_id: int) -> str:
        """Returns 'apply' | 'skip' | 'resync' for a diff event (U, u)."""
        if self.last_update_id is None:
            return "resync"
        if not self.synced:
            if final_id <= self.last_update_id:
                return "skip"  # event predates snapshot
            if first_id <= self.last_update_id + 1 <= final_id:
                self.synced = True
                self.last_update_id = final_id
                return "apply"
            return "resync"  # snapshot too old: missed the bridge
        if first_id == self.last_update_id + 1:
            self.last_update_id = final_id
            return "apply"
        if final_id <= self.last_update_id:
            return "skip"
        return "resync"


class Recorder:
    def __init__(self, symbols: list[str], out_dir: Path) -> None:
        self.symbols = symbols
        self.out_dir = out_dir
        self.buffers: dict[str, _HourBuffer] = {s: _HourBuffer() for s in symbols}
        self.sequencers: dict[str, DepthSequencer] = {s: DepthSequencer() for s in symbols}
        self.hour_started_at: datetime = datetime.now(UTC)
        self.connected_seconds: float = 0.0
        self._stop = asyncio.Event()

    # -- snapshot / resync -------------------------------------------------

    async def _resync(self, symbol: str, client: httpx.AsyncClient) -> None:
        params = {"symbol": symbol.replace("/", ""), "limit": _SNAPSHOT_LIMIT}
        resp = await client.get(_REST_SNAPSHOT, params=params, timeout=10)
        resp.raise_for_status()
        snap = resp.json()
        self.sequencers[symbol].apply_snapshot(int(snap["lastUpdateId"]))
        buf = self.buffers[symbol]
        buf.resyncs += 1
        buf.depth.append(
            {
                "event": "snapshot",
                "ts_ms": int(datetime.now(UTC).timestamp() * 1000),
                "last_update_id": int(snap["lastUpdateId"]),
                "bids": json.dumps(snap["bids"][:20]),
                "asks": json.dumps(snap["asks"][:20]),
                "first_id": None,
                "final_id": None,
            }
        )
        logger.info("ftr_recorder_resync", symbol=symbol, last_update_id=snap["lastUpdateId"])

    # -- event handling ----------------------------------------------------

    def handle_event(self, symbol: str, channel: str, data: dict[str, Any]) -> bool:
        """Buffer one event. Returns True if a depth resync is required."""
        buf = self.buffers[symbol]
        ts_ms = int(data.get("E", datetime.now(UTC).timestamp() * 1000))
        if buf.first_event_ms is None:
            buf.first_event_ms = ts_ms
        buf.last_event_ms = ts_ms

        if channel == "depth":
            seq = self.sequencers[symbol]
            verdict = seq.check(int(data["U"]), int(data["u"]))
            if verdict == "skip":
                return False
            if verdict == "resync":
                buf.sequence_gaps += 1
                return True
            buf.depth.append(
                {
                    "event": "diff",
                    "ts_ms": ts_ms,
                    "first_id": int(data["U"]),
                    "final_id": int(data["u"]),
                    "bids": json.dumps(data.get("b", [])),
                    "asks": json.dumps(data.get("a", [])),
                    "last_update_id": None,
                }
            )
        elif channel == "aggTrade":
            buf.trades.append(
                {
                    "ts_ms": ts_ms,
                    "price": float(data["p"]),
                    "qty": float(data["q"]),
                    "is_buyer_maker": bool(data["m"]),
                    "agg_id": int(data["a"]),
                }
            )
        elif channel == "bookTicker":
            buf.book_ticker.append(
                {
                    "ts_ms": ts_ms,
                    "bid": float(data["b"]),
                    "bid_qty": float(data["B"]),
                    "ask": float(data["a"]),
                    "ask_qty": float(data["A"]),
                }
            )
        return False

    # -- rotation ----------------------------------------------------------

    def rotate(self, *, now: datetime | None = None) -> list[Path]:
        """Write the current hour's buffers + manifest; reset. Returns paths."""
        now = now or datetime.now(UTC)
        stamp = self.hour_started_at.strftime("%Y%m%dT%H")
        written: list[Path] = []
        hour_span_s = max((now - self.hour_started_at).total_seconds(), 1.0)
        for symbol, buf in self.buffers.items():
            safe = symbol.replace("/", "_")
            base = self.out_dir / safe / stamp
            base.mkdir(parents=True, exist_ok=True)
            for name, rows in (
                ("depth", buf.depth),
                ("trades", buf.trades),
                ("book_ticker", buf.book_ticker),
            ):
                if rows:
                    path = base / f"{name}.parquet"
                    pd.DataFrame(rows).to_parquet(path, engine="pyarrow", compression="snappy")
                    written.append(path)
            manifest = {
                "symbol": symbol,
                "hour": stamp,
                "depth_events": len(buf.depth),
                "trade_events": len(buf.trades),
                "book_ticker_events": len(buf.book_ticker),
                "sequence_gaps": buf.sequence_gaps,
                "resyncs": buf.resyncs,
                "uptime_pct": round(min(self.connected_seconds / hour_span_s, 1.0) * 100, 2),
                "written_at": now.isoformat(),
            }
            mpath = base / "manifest.json"
            mpath.write_text(json.dumps(manifest, indent=2))
            written.append(mpath)
        self.buffers = {s: _HourBuffer() for s in self.symbols}
        self.hour_started_at = now
        self.connected_seconds = 0.0
        return written

    # -- main loop ---------------------------------------------------------

    async def run(self) -> None:
        import websockets

        streams = "/".join(
            f"{_stream_name(s)}@{ch}"
            for s in self.symbols
            for ch in ("depth@100ms", "aggTrade", "bookTicker")
        )
        url = f"{_WS_BASE}?streams={streams}"
        async with httpx.AsyncClient() as http:
            for s in self.symbols:
                await self._resync(s, http)
            events_since_flush = 0
            while not self._stop.is_set():
                try:
                    async with websockets.connect(url, ping_interval=20) as ws:
                        logger.info("ftr_recorder_connected", symbols=self.symbols)
                        connect_t = asyncio.get_event_loop().time()
                        async for raw in ws:
                            msg = json.loads(raw)
                            stream = msg.get("stream", "")
                            data = msg.get("data", {})
                            sym = next(
                                (s for s in self.symbols if stream.startswith(_stream_name(s))),
                                None,
                            )
                            if sym is None:
                                continue
                            channel = (
                                "depth"
                                if "depth" in stream
                                else "aggTrade"
                                if "aggTrade" in stream
                                else "bookTicker"
                            )
                            if self.handle_event(sym, channel, data):
                                await self._resync(sym, http)
                            events_since_flush += 1
                            now_t = asyncio.get_event_loop().time()
                            self.connected_seconds += now_t - connect_t
                            connect_t = now_t
                            if (
                                datetime.now(UTC) - self.hour_started_at
                            ).total_seconds() >= _ROTATE_S:
                                self.rotate()
                            if self._stop.is_set():
                                break
                except Exception as exc:
                    logger.warning("ftr_recorder_disconnect", err=str(exc))
                    await asyncio.sleep(2.0)
                    for s in self.symbols:
                        try:
                            await self._resync(s, http)
                        except httpx.HTTPError as snap_exc:
                            logger.warning("ftr_recorder_resync_failed", err=str(snap_exc))
            self.rotate()

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    from marketmind_workers.ftr.config.settings import get_ftr_settings

    settings = get_ftr_settings()
    symbols = [s.strip() for s in settings.recorder_symbols.split(",") if s.strip()]
    if settings.recorder_record_eth and "ETH/USDT" not in symbols:
        symbols.append("ETH/USDT")
    out_dir = settings.recordings_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(symbols, out_dir)

    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, recorder.stop)
    logger.info("ftr_recorder_start", symbols=symbols, out_dir=str(out_dir))
    loop.run_until_complete(recorder.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
