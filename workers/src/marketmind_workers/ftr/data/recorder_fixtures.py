"""FIXTURE — NOT MARKET DATA.

Synthetic L1/L2 event generators for unit tests of the recorder, the
microstructure feature pipeline, and the OFI research module. Every frame
produced here carries a ``fixture`` marker column. These fixtures exist so
the code paths are testable without network access; they are NEVER used for
performance claims, calibration, or verdicts of any kind.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

FIXTURE_MARKER = "FIXTURE — NOT MARKET DATA"


def _rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(seed)


def make_book_ticker_fixture(
    *,
    start: datetime | None = None,
    n_events: int = 2000,
    mid0: float = 60_000.0,
    spread_bps: float = 1.2,
    seed: int = 7,
) -> pd.DataFrame:
    """Synthetic best bid/ask stream on a ~1s grid with a random-walk mid."""
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    rng = _rng(seed)
    ts = np.array(
        [int((start + timedelta(seconds=i)).timestamp() * 1000) for i in range(n_events)]
    )
    mid = mid0 * np.exp(np.cumsum(rng.normal(0, 2e-5, n_events)))
    half = mid * (spread_bps / 2.0) * 1e-4
    df = pd.DataFrame(
        {
            "ts_ms": ts,
            "bid": mid - half,
            "bid_qty": rng.uniform(0.5, 5.0, n_events),
            "ask": mid + half,
            "ask_qty": rng.uniform(0.5, 5.0, n_events),
        }
    )
    df["fixture"] = FIXTURE_MARKER
    return df


def make_agg_trades_fixture(
    *,
    start: datetime | None = None,
    n_events: int = 3000,
    mid0: float = 60_000.0,
    seed: int = 11,
) -> pd.DataFrame:
    """Synthetic aggTrade stream; buyer-maker flag drives signed flow."""
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    rng = _rng(seed)
    offsets = np.sort(rng.uniform(0, n_events * 0.7, n_events))
    ts = np.array([int((start + timedelta(seconds=float(o))).timestamp() * 1000) for o in offsets])
    price = mid0 * np.exp(np.cumsum(rng.normal(0, 3e-5, n_events)))
    df = pd.DataFrame(
        {
            "ts_ms": ts,
            "price": price,
            "qty": rng.lognormal(-2.0, 1.0, n_events),
            "is_buyer_maker": rng.uniform(size=n_events) < 0.5,
            "agg_id": np.arange(n_events, dtype="int64"),
        }
    )
    df["fixture"] = FIXTURE_MARKER
    return df


def make_depth_events_fixture(
    *,
    last_update_id: int = 1000,
    n_events: int = 500,
    inject_gap_at: int | None = None,
    mid0: float = 60_000.0,
    seed: int = 13,
) -> list[dict[str, object]]:
    """Synthetic Binance diff-depth payloads with optional sequence gap.

    Returns raw dicts shaped like the websocket `data` field (U, u, b, a, E)
    so DepthSequencer logic can be tested without a connection.
    """
    rng = _rng(seed)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    events: list[dict[str, object]] = []
    cur = last_update_id + 1
    for i in range(n_events):
        if inject_gap_at is not None and i == inject_gap_at:
            cur += 50  # simulate lost events
        n_updates = int(rng.integers(1, 5))
        mid = mid0 * (1 + rng.normal(0, 1e-4))
        events.append(
            {
                "E": int((start + timedelta(milliseconds=100 * i)).timestamp() * 1000),
                "U": cur,
                "u": cur + n_updates - 1,
                "b": [[f"{mid - 1:.2f}", f"{rng.uniform(0.1, 3.0):.4f}"]],
                "a": [[f"{mid + 1:.2f}", f"{rng.uniform(0.1, 3.0):.4f}"]],
            }
        )
        cur += n_updates
    return events
