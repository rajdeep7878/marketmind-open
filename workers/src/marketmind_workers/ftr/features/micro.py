"""Microstructure features from recorder output (mandate Stage 2).

Base grid: 1s, aggregated to 10s and 1m. Inputs are the recorder's
``book_ticker`` (L1), ``depth`` (L2 diffs + snapshots) and ``trades``
(aggTrade) parquet outputs.

Order-flow imbalance follows the event definition of Cont, Kukanov &
Stoikov (2014), "The Price Impact of Order Book Events": for consecutive
L1 observations (P_b, q_b, P_a, q_a),

    e_n =  q_b(n)·1[P_b(n) >= P_b(n-1)] − q_b(n−1)·1[P_b(n) <= P_b(n-1)]
         − q_a(n)·1[P_a(n) <= P_a(n-1)] + q_a(n−1)·1[P_a(n) >= P_a(n-1)]

summed per window. Realized-spread / adverse-selection markouts at 30s, 1m,
5m measure what a taker actually pays after the mid moves against them.

All functions accept fixture frames in tests; performance claims only ever
come from real recordings (enforced by the Stage-4 INSUFFICIENT_DATA gate).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MARKOUT_HORIZONS_S: tuple[int, ...] = (30, 60, 300)


def _to_ts_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts_ms"], unit="ms", utc=True)
    return out.set_index("ts").sort_index()


def l1_grid(book_ticker: pd.DataFrame, *, freq: str = "1s") -> pd.DataFrame:
    """Resample raw bookTicker events onto a regular grid (last obs per bin).

    Empty bins stay NaN — a missing L1 observation is missing, not carried.
    """
    bt = _to_ts_frame(book_ticker)
    grid = bt[["bid", "bid_qty", "ask", "ask_qty"]].resample(freq).last()
    grid["mid"] = (grid["bid"] + grid["ask"]) / 2.0
    grid["spread_bps"] = (grid["ask"] - grid["bid"]) / grid["mid"] * 1e4
    grid["l1_imbalance"] = (grid["bid_qty"] - grid["ask_qty"]) / (
        grid["bid_qty"] + grid["ask_qty"]
    )
    return grid


def ofi_events(l1: pd.DataFrame) -> pd.Series:
    """Per-observation CKS order-flow imbalance e_n on an L1 grid."""
    pb, qb = l1["bid"], l1["bid_qty"]
    pa, qa = l1["ask"], l1["ask_qty"]
    pb_prev, qb_prev = pb.shift(1), qb.shift(1)
    pa_prev, qa_prev = pa.shift(1), qa.shift(1)
    e = (
        qb * (pb >= pb_prev).astype(float)
        - qb_prev * (pb <= pb_prev).astype(float)
        - qa * (pa <= pa_prev).astype(float)
        + qa_prev * (pa >= pa_prev).astype(float)
    )
    return e.rename("ofi_event")


def micro_features(
    book_ticker: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    window: str = "10s",
) -> pd.DataFrame:
    """Windowed microstructure features on the requested aggregation grid."""
    l1 = l1_grid(book_ticker, freq="1s")
    ofi = ofi_events(l1)

    tr = _to_ts_frame(trades)
    # aggTrade: is_buyer_maker=True means the AGGRESSOR was a seller.
    signed_qty = tr["qty"] * np.where(tr["is_buyer_maker"], -1.0, 1.0)
    tfi_n = signed_qty.resample(window).sum()
    vol_n = tr["qty"].resample(window).sum()

    mid = l1["mid"]
    logmid = pd.Series(np.log(mid.to_numpy(dtype="float64")), index=mid.index)

    return pd.DataFrame(
        {
            "spread_bps": l1["spread_bps"].resample(window).mean(),
            "mid_logret": logmid.resample(window).last().diff(),
            "l1_imbalance": l1["l1_imbalance"].resample(window).mean(),
            "ofi": ofi.resample(window).sum(),
            "tfi": tfi_n,
            "signed_volume": tfi_n,  # alias: signed base-asset volume
            "volume": vol_n,
            "rvol_short": logmid.diff().resample(window).std(),
        }
    )


def depth_imbalance(depth_snapshot_bids: list[list[str]], depth_snapshot_asks: list[list[str]], levels: int) -> float:
    """L-level depth imbalance from one snapshot's [price, qty] lists."""
    bid_qty = sum(float(q) for _, q in depth_snapshot_bids[:levels])
    ask_qty = sum(float(q) for _, q in depth_snapshot_asks[:levels])
    total = bid_qty + ask_qty
    return 0.0 if total == 0 else (bid_qty - ask_qty) / total


def markouts(
    book_ticker: pd.DataFrame,
    *,
    horizons_s: tuple[int, ...] = MARKOUT_HORIZONS_S,
) -> pd.DataFrame:
    """Taker markouts: half-spread paid vs mid drift over each horizon (bps).

    ``adverse_selection_<h>s``: mid(t+h)/mid(t)-1 in bps signed for a BUYER
    (positive = price ran away after the fill — the taker bought ahead of a
    rise, good; negative = adverse). ``realized_spread_<h>s``: effective
    half-spread minus the favorable drift — what crossing actually cost.
    """
    l1 = l1_grid(book_ticker, freq="1s")
    mid = l1["mid"]
    half_spread_bps = l1["spread_bps"] / 2.0
    out: dict[str, pd.Series] = {"half_spread_bps": half_spread_bps}
    for h in horizons_s:
        drift_bps = (mid.shift(-h) / mid - 1.0) * 1e4
        out[f"adverse_selection_{h}s"] = drift_bps
        out[f"realized_spread_{h}s"] = half_spread_bps - drift_bps
    return pd.DataFrame(out)
