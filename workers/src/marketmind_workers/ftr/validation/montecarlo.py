"""Monte Carlo / randomization tests (mandate Stage 4).

(a) block bootstrap of trade PnL (block = 24h) x 1000 => CI on
    Sharpe-proxy and expectancy;
(b) matched-frequency random-entry simulation x 1000 — same trades/day,
    same average holding time, same instrument and costs;
(c) ML only: label-permutation refit (block-permuted labels) — the real
    model's OOS metric must beat the permuted 95th percentile (implemented
    in validation.runner where the fold machinery lives).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from marketmind_workers.ftr.backtest.costs import CostBreakdown
from marketmind_workers.ftr.backtest.vector_engine import Trade
from marketmind_workers.ftr.data.ohlcv import dtindex


@dataclass(frozen=True)
class BootstrapCI:
    n_paths: int
    expectancy_p05: float
    expectancy_p50: float
    expectancy_p95: float
    sharpe_proxy_p05: float
    sharpe_proxy_p50: float
    sharpe_proxy_p95: float


def block_bootstrap_trades(
    trades: list[Trade],
    *,
    n_paths: int = 1000,
    seed: int = 1729,
) -> BootstrapCI | None:
    """Resample trade net returns in 24h blocks; CI on expectancy + Sharpe.

    The Sharpe proxy is per-trade (mean/std of trade returns) — it preserves
    ordering under resampling without re-annualizing each path.
    """
    if len(trades) < 5:
        return None
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(
        {
            "day": [t.entry_ts.strftime("%Y-%m-%d") for t in trades],
            "ret": [t.net_return for t in trades],
        }
    )
    by_day = [g["ret"].to_numpy() for _, g in frame.groupby("day")]
    n_blocks = len(by_day)
    exp_paths = np.empty(n_paths)
    sharpe_paths = np.empty(n_paths)
    for p in range(n_paths):
        picks = rng.integers(0, n_blocks, n_blocks)
        rets = np.concatenate([by_day[i] for i in picks])
        exp_paths[p] = rets.mean()
        sd = rets.std(ddof=1) if len(rets) > 2 else 0.0
        sharpe_paths[p] = rets.mean() / sd if sd > 0 else 0.0
    return BootstrapCI(
        n_paths=n_paths,
        expectancy_p05=float(np.percentile(exp_paths, 5)),
        expectancy_p50=float(np.percentile(exp_paths, 50)),
        expectancy_p95=float(np.percentile(exp_paths, 95)),
        sharpe_proxy_p05=float(np.percentile(sharpe_paths, 5)),
        sharpe_proxy_p50=float(np.percentile(sharpe_paths, 50)),
        sharpe_proxy_p95=float(np.percentile(sharpe_paths, 95)),
    )


@dataclass(frozen=True)
class RandomEntryResult:
    n_sims: int
    real_net_return: float
    sim_p50: float
    sim_p95: float
    percentile_of_real: float  # fraction of sims the real return beats


def matched_frequency_random_entries(
    ohlcv: pd.DataFrame,
    *,
    n_trades: int,
    avg_hold_bars: int,
    costs: CostBreakdown,
    real_net_return: float,
    window_index: pd.DatetimeIndex | None = None,
    n_sims: int = 1000,
    seed: int = 1729,
) -> RandomEntryResult | None:
    """Random entries matched on count + holding time, same costs.

    Entries are sampled uniformly over the same OOS window the strategy
    traded; overlapping holds are discarded and resampled order-free (the
    sim is long/flat like the strategy). Net return per sim compounds
    open->open fills with per-side costs — identical fill law.
    """
    if n_trades < 1 or avg_hold_bars < 1:
        return None
    idx = dtindex(ohlcv)
    if window_index is not None:
        positions = idx.get_indexer(window_index)
        candidate = positions[positions >= 0]
    else:
        candidate = np.arange(len(idx))
    open_arr = ohlcv["open"].to_numpy(dtype="float64")
    n_bars = len(open_arr)
    side_cost = costs.per_side_bps * 1e-4
    hi = candidate.max() - avg_hold_bars - 1
    lo = candidate.min()
    if hi <= lo:
        return None

    rng = np.random.default_rng(seed)
    sims = np.empty(n_sims)
    for s in range(n_sims):
        entries = np.sort(rng.integers(lo, hi, n_trades * 3))
        chosen: list[int] = []
        last_exit = -1
        for e in entries:
            if e > last_exit:
                chosen.append(int(e))
                last_exit = e + avg_hold_bars
                if len(chosen) == n_trades:
                    break
        eq = 1.0
        for e in chosen:
            x = min(e + avg_hold_bars, n_bars - 1)
            entry_fill = open_arr[e + 1] * (1.0 + side_cost)
            exit_fill = open_arr[min(x + 1, n_bars - 1)] * (1.0 - side_cost)
            eq *= exit_fill / entry_fill
        sims[s] = eq - 1.0

    pct = float((sims < real_net_return).mean())
    return RandomEntryResult(
        n_sims=n_sims,
        real_net_return=real_net_return,
        sim_p50=float(np.percentile(sims, 50)),
        sim_p95=float(np.percentile(sims, 95)),
        percentile_of_real=pct,
    )


def block_permute_labels(
    y: pd.Series,
    *,
    block_bars: int = 168,
    seed: int = 0,
) -> pd.Series:
    """Permute labels in contiguous blocks (preserves label autocorrelation
    while destroying any feature-label relationship)."""
    rng = np.random.default_rng(seed)
    arr = y.to_numpy().copy()
    n = len(arr)
    n_blocks = int(np.ceil(n / block_bars))
    order = rng.permutation(n_blocks)
    blocks = [arr[i * block_bars : (i + 1) * block_bars] for i in range(n_blocks)]
    permuted = np.concatenate([blocks[i] for i in order])[:n]
    return pd.Series(permuted, index=y.index, name=y.name)
