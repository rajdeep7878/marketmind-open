"""Monte Carlo permutation test.

Null hypothesis: "the strategy's return would be the same on random
data that has the same per-bar return distribution as the real data
(preserved by shuffling) but no time-series structure (destroyed by
shuffling)."

If the strategy's edge comes from real market dynamics (trend, mean
reversion, autocorrelation), then on shuffled-return data — which has
the same volatility but is i.i.d. by construction — the strategy
should perform much worse on average. The fraction of shuffled
realisations that beat the real return is the p-value of the
strategy's edge.

The shuffle is applied to log returns, not raw prices. This preserves:
  - the empirical return distribution (mean, std, fat tails)
  - the cumulative drift over the period
  - the price scale (we re-anchor to the real open price)

And destroys:
  - any autocorrelation
  - regime structure (trending phases, drawdowns, mean-reverting
    pockets)

If the strategy depends on those time-series features (almost all
real strategies do), it will collapse on the permutations.

Compute budget: 100 backtests at ~1s each on cached data ≈ 100s.
Falls back to 50 permutations if the spec is slow enough that 100
would exceed 2 minutes; the threshold is the first permutation's
wall-clock time.
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
import structlog
from marketmind_shared.schemas import (
    MonteCarloHistogramBin,
    MonteCarloResult,
    StrategySpec,
)
from marketmind_shared.schemas.strategy_spec.common import Timeframe

from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.metrics import compute_metrics
from marketmind_workers.services.market_data import get_market_data

log = structlog.get_logger(__name__)


_DEFAULT_PERMUTATIONS: Final[int] = 100
_FALLBACK_PERMUTATIONS: Final[int] = 50
# Time budget: if the first permutation takes longer than this many
# seconds, fall back to fewer permutations so the whole sweep stays
# under ~2 minutes.
_TIME_PER_PERMUTATION_BUDGET_S: Final[float] = 1.2
_HISTOGRAM_BINS: Final[int] = 24
_DEFAULT_SEED: Final[int] = 42


def run_monte_carlo(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    *,
    n_permutations: int = _DEFAULT_PERMUTATIONS,
    seed: int = _DEFAULT_SEED,
    initial_capital: float = 10_000.0,
    data_dir: str | Path = "/data",
) -> MonteCarloResult:
    """Run the permutation test. Pulls the real OHLCV once and
    re-shuffles its returns `n_permutations` times.
    """
    if n_permutations < 2:
        raise ValueError(f"n_permutations must be >= 2; got {n_permutations}")

    # ---- real run (baseline) -------------------------------------------------
    real_data = _load_data(spec, start, end, data_dir)
    try:
        real_run = run_backtest(
            spec,
            start,
            end,
            initial_capital,
            data_override=real_data,
        )
        real_metrics = compute_metrics(real_run, spec.primary_timeframe)
        real_return = real_metrics.total_return_pct
        real_sharpe = real_metrics.sharpe_ratio
    except Exception as exc:
        log.warning("monte_carlo_real_run_failed", error=str(exc))
        real_return = 0.0
        real_sharpe = 0.0

    # ---- permutations --------------------------------------------------------
    rng = np.random.default_rng(seed)
    primary_df = real_data[spec.primary_timeframe]
    log_returns = _log_returns(primary_df)

    synth_returns: list[float] = []
    first_t = time.perf_counter()
    permutations_target = n_permutations

    for i in range(n_permutations):
        permutations_attempted = i + 1
        shuffled = rng.permutation(log_returns)
        synth_primary = _reconstruct(primary_df, shuffled)
        synth_data = dict(real_data)  # shallow copy
        synth_data[spec.primary_timeframe] = synth_primary
        # Filter timeframe — if used — also gets independent shuffling
        # so the null hypothesis on each timeframe is consistent.
        if spec.filter_timeframe is not None and spec.filter_timeframe in real_data:
            filter_df = real_data[spec.filter_timeframe]
            filter_returns = _log_returns(filter_df)
            shuffled_f = rng.permutation(filter_returns)
            synth_data[spec.filter_timeframe] = _reconstruct(filter_df, shuffled_f)

        try:
            run = run_backtest(
                spec,
                start,
                end,
                initial_capital,
                data_override=synth_data,
            )
            metrics = compute_metrics(run, spec.primary_timeframe)
            synth_returns.append(metrics.total_return_pct)
        except Exception as exc:
            log.info("monte_carlo_permutation_failed", i=i, error=str(exc))
            synth_returns.append(0.0)

        # Time-budget check after the first permutation. If too slow,
        # cap the loop at the fallback count.
        if i == 0:
            elapsed = time.perf_counter() - first_t
            if elapsed > _TIME_PER_PERMUTATION_BUDGET_S and n_permutations > _FALLBACK_PERMUTATIONS:
                permutations_target = _FALLBACK_PERMUTATIONS
                log.warning(
                    "monte_carlo_fallback_n",
                    first_permutation_seconds=elapsed,
                    new_target=permutations_target,
                )
        if permutations_attempted >= permutations_target:
            break

    return _build_result(
        real_return=real_return,
        real_sharpe=real_sharpe,
        synth_returns=synth_returns,
        seed=seed,
    )


def _load_data(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    data_dir: str | Path,
) -> dict[Timeframe, pd.DataFrame]:
    """Load OHLCV for the spec's timeframes. Used both for the real
    run and as the basis for the permutations.
    """
    needed: list[Timeframe] = [spec.primary_timeframe]
    if spec.filter_timeframe is not None:
        needed.append(spec.filter_timeframe)
    out: dict[Timeframe, pd.DataFrame] = {}
    for tf in needed:
        out[tf] = get_market_data(spec.instrument.symbol, tf.value, start, end, data_dir=data_dir)
    return out


def _log_returns(df: pd.DataFrame) -> np.ndarray:
    """Log returns from the close column. Drops the leading NaN."""
    closes = df["close"].to_numpy(dtype="float64")
    if closes.size < 2:
        return np.zeros(0, dtype="float64")
    return np.diff(np.log(closes))


def _reconstruct(real_df: pd.DataFrame, shuffled_log_returns: np.ndarray) -> pd.DataFrame:
    """Re-build an OHLCV frame from shuffled log returns.

    Anchors at the real first-bar open so the price scale matches.
    Open / high / low / close on the synthetic frame are all set to
    the reconstructed close — we don't try to fabricate intra-bar
    structure that would be meaningful for stop hits, because the
    permutation test is about return distribution, not bar shape.
    Volume is copied from the real frame so any volume-based filters
    behave plausibly.
    """
    if shuffled_log_returns.size == 0:
        return real_df.copy()
    first_open = float(real_df["open"].iloc[0])
    # closes[0] = first_open; closes[i] = closes[i-1] * exp(shuffled[i-1])
    closes = np.empty(len(real_df), dtype="float64")
    closes[0] = first_open
    closes[1:] = first_open * np.exp(np.cumsum(shuffled_log_returns))
    volume = real_df["volume"].to_numpy(dtype="float64")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": volume,
        },
        index=real_df.index,
    )


def _build_result(
    *,
    real_return: float,
    real_sharpe: float,
    synth_returns: list[float],
    seed: int,
) -> MonteCarloResult:
    if not synth_returns:
        # Pathological — every permutation failed. Surface as a wide
        # uninformative distribution so the composite doesn't crash.
        return MonteCarloResult(
            real_return_pct=real_return,
            real_sharpe=real_sharpe,
            n_permutations=0,
            synthetic_mean_return=0.0,
            synthetic_std_return=0.0,
            synthetic_min=0.0,
            synthetic_max=0.0,
            histogram=[MonteCarloHistogramBin(lo=0.0, hi=1.0, count=0)],
            p_value=1.0,
            percentile_rank=0.0,
            seed=seed,
        )

    arr = np.asarray(synth_returns, dtype="float64")
    n = arr.size
    mean = float(arr.mean())
    std = float(arr.std(ddof=1)) if n > 1 else 0.0
    mn = float(arr.min())
    mx = float(arr.max())

    # P(synthetic >= real). Bias to be conservative on ties: count
    # >= rather than >, so a tied real and synth doesn't claim
    # significance.
    p_value = float((arr >= real_return).sum()) / n
    percentile_rank = float((arr < real_return).sum()) / n

    histogram = _build_histogram(arr)

    return MonteCarloResult(
        real_return_pct=real_return,
        real_sharpe=real_sharpe,
        n_permutations=n,
        synthetic_mean_return=mean,
        synthetic_std_return=std,
        synthetic_min=mn,
        synthetic_max=mx,
        histogram=histogram,
        p_value=p_value,
        percentile_rank=percentile_rank,
        seed=seed,
    )


def _build_histogram(values: np.ndarray) -> list[MonteCarloHistogramBin]:
    """Build a uniform-width histogram of synthetic returns for the UI.

    Width capped at _HISTOGRAM_BINS bins. When all values are equal,
    return a single bin centred on that value so the response remains
    valid.
    """
    lo_val = float(values.min())
    hi_val = float(values.max())
    if math.isclose(lo_val, hi_val):
        return [MonteCarloHistogramBin(lo=lo_val, hi=lo_val + 1e-9, count=int(values.size))]
    counts, edges = np.histogram(values, bins=_HISTOGRAM_BINS, range=(lo_val, hi_val))
    bins: list[MonteCarloHistogramBin] = []
    for i, c in enumerate(counts):
        bins.append(
            MonteCarloHistogramBin(
                lo=float(edges[i]),
                hi=float(edges[i + 1]),
                count=int(c),
            ),
        )
    return bins


__all__ = ["run_monte_carlo"]
