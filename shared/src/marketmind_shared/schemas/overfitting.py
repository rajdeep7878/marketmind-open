"""Phase 4 overfitting analysis schemas.

The four analyses + the composite score that sits on top of them are
the differentiator of MarketMind — the answer to "looks great in
backtest, but is it actually a real edge?". Schema design here drives
both the persistence shape (`overfitting_analyses` table JSONB blobs)
and the UI consumption.

Single file for all five sub-schemas + the top-level Analysis because
they always travel together (one analysis is one row, written
atomically).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import _StrictModel


def _require_utc(field_name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise PydanticCustomError(
            "datetime_must_be_utc",
            "{field} must be timezone-aware UTC; got naive datetime",
            {"field": field_name},
        )
    if value.utcoffset() != timedelta(0):
        raise PydanticCustomError(
            "datetime_must_be_utc",
            "{field} must be UTC (offset 0)",
            {"field": field_name},
        )
    return value.astimezone(UTC)


# ---- Walk-forward ----------------------------------------------------------


class WindowResult(_StrictModel):
    """One walk-forward window: in-sample stats + out-of-sample stats."""

    window_index: int = Field(ge=0)
    in_sample_start: datetime
    in_sample_end: datetime
    out_of_sample_start: datetime
    out_of_sample_end: datetime
    in_sample_return_pct: float
    in_sample_sharpe: float
    in_sample_num_trades: int = Field(ge=0)
    out_of_sample_return_pct: float
    out_of_sample_sharpe: float
    out_of_sample_num_trades: int = Field(ge=0)

    @field_validator("in_sample_start", "in_sample_end", "out_of_sample_start", "out_of_sample_end")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _require_utc("window timestamp", v)


class WalkForwardResult(_StrictModel):
    """Aggregate result over n rolling windows.

    `degradation_ratio = OOS_avg / IS_avg`. 1.0 == perfect consistency,
    < 0.5 == serious degradation. Set to 0.0 (with `degradation_ratio_valid=False`)
    when IS average is non-positive — in that case the strategy didn't
    work in-sample and the OOS comparison is meaningless.

    `consistency_score` in [0, 1] derived from std of OOS returns —
    1.0 == every window posted the same return, 0.0 == wildly different.
    """

    windows: list[WindowResult]
    in_sample_avg_return: float
    out_of_sample_avg_return: float
    degradation_ratio: float
    degradation_ratio_valid: bool = True
    out_of_sample_positive_rate: float = Field(ge=0.0, le=1.0)
    consistency_score: float = Field(ge=0.0, le=1.0)
    train_ratio: float = Field(gt=0.0, lt=1.0)
    n_windows_requested: int = Field(ge=1)
    n_windows_actual: int = Field(ge=0)


# ---- Parameter sweep -------------------------------------------------------


class SweepAxisKind(StrEnum):
    """Which parameter of the spec is being swept.

    v1 list — Phase 5 may extend. Adding a value here requires also
    extending the worker's parameter_sweep.py to know how to mutate
    the spec.
    """

    STOP_LOSS_PCT = "stop_loss_pct"
    TAKE_PROFIT_PCT = "take_profit_pct"
    INDICATOR_PERIOD = "indicator_period"
    RSI_LOWER_THRESHOLD = "rsi_lower_threshold"
    RSI_UPPER_THRESHOLD = "rsi_upper_threshold"
    # v1.2.A: PercentileExpr.window (rolling-percentile lookback). Mutated
    # at JSON paths pointing to a `kind="percentile"` expression's `window`
    # field. Discrete int neighborhood like INDICATOR_PERIOD.
    PERCENTILE_WINDOW = "percentile_window"


class SweepAxis(_StrictModel):
    """One axis of the parameter grid.

    `target_path` is a JSON-pointer-style path into the spec dict so
    the mutator knows exactly where to assign. Stored alongside the
    enum kind because some axes map to multiple positions (e.g., RSI
    period appears in both entry and exit conditions).
    """

    kind: SweepAxisKind
    label: str = Field(min_length=1, max_length=120)
    values: list[float] = Field(min_length=1, max_length=12)
    baseline_value: float
    target_paths: list[str] = Field(min_length=1, max_length=8)


class SweepCell(_StrictModel):
    """One run of the sweep — a specific parameter combination."""

    axis_values: dict[str, float]  # axis label -> value
    total_return_pct: float
    sharpe_ratio: float
    num_trades: int = Field(ge=0)
    is_baseline: bool = False


class ParameterSweepResult(_StrictModel):
    """The full grid + summary stats.

    `peakiness_score` in [0, 1]: 0 == baseline sits on a robust plateau
    (returns similar to neighbors), 1 == baseline is a sharp lone peak
    much higher than its immediate neighbors (overfitting signal).
    """

    axes: list[SweepAxis]
    cells: list[SweepCell]
    baseline_return_pct: float
    baseline_rank_percentile: float = Field(ge=0.0, le=1.0)
    best_in_grid_return: float
    worst_in_grid_return: float
    neighborhood_avg_return: float
    peakiness_score: float = Field(ge=0.0, le=1.0)
    n_combinations: int = Field(ge=0)
    skipped_reason: str | None = Field(default=None, max_length=500)


# ---- Monte Carlo ----------------------------------------------------------


class MonteCarloHistogramBin(_StrictModel):
    """One bin in the synthetic-returns histogram (serialised for the UI)."""

    lo: float
    hi: float
    count: int = Field(ge=0)


class MonteCarloResult(_StrictModel):
    """Permutation-test result.

    Null hypothesis: the strategy's returns would be the same on
    random data with the same per-bar return distribution (preserved
    by shuffling) but no time-series structure (destroyed by shuffling).

    `p_value = P(synthetic_return >= real_return)`. Small p (< 0.05) →
    the strategy's edge is unlikely to be a fluke of price ordering.

    `percentile_rank` = fraction of synthetic returns strictly below
    the real return. 1.0 == real beats every permutation, 0.0 == real
    is the worst result we saw.
    """

    real_return_pct: float
    real_sharpe: float
    n_permutations: int = Field(ge=1)
    synthetic_mean_return: float
    synthetic_std_return: float
    synthetic_min: float
    synthetic_max: float
    histogram: list[MonteCarloHistogramBin] = Field(min_length=1, max_length=64)
    p_value: float = Field(ge=0.0, le=1.0)
    percentile_rank: float = Field(ge=0.0, le=1.0)
    seed: int


# ---- Deflated Sharpe -------------------------------------------------------


class DeflatedSharpeResult(_StrictModel):
    """Bailey & López de Prado (2014) Deflated Sharpe Ratio.

    `probability_strategy_is_real` is the user-facing number — the
    probability that the true Sharpe is greater than the expected
    maximum of N i.i.d. trials' Sharpes, given the observed Sharpe,
    sample size, and returns shape. Close to 1.0 == almost certainly
    a real edge; close to 0.0 == almost certainly a selection artefact.

    `method` flags which formula was used so future re-analyses can
    detect a v1 haircut vs the full LdP form.
    """

    observed_sharpe: float
    deflated_sharpe_ratio: float
    probability_strategy_is_real: float = Field(ge=0.0, le=1.0)
    n_trials_estimate: int = Field(ge=1)
    n_observations: int = Field(ge=2)
    returns_skewness: float
    returns_kurtosis: float
    expected_max_sharpe: float
    method: Literal["lopez_de_prado_full", "haircut_v1"]


# ---- Composite -------------------------------------------------------------


class OverfittingVerdict(StrEnum):
    LIKELY_ROBUST = "likely_robust"
    MIXED_SIGNALS = "mixed_signals"
    LIKELY_OVERFIT = "likely_overfit"


class SignalContribution(_StrictModel):
    """One of the four input signals' contribution to the composite.

    `raw_value` is the analysis's headline number (degradation_ratio,
    peakiness_score, p_value, probability_strategy_is_real).
    `contribution_pts` is the 0-100 contribution AFTER weighting.
    """

    name: Literal["walk_forward", "parameter_sweep", "monte_carlo", "deflated_sharpe"]
    label: str = Field(min_length=1, max_length=120)
    raw_value: float
    weight: float = Field(ge=0.0, le=1.0)
    contribution_pts: float = Field(ge=0.0, le=100.0)


class OverfittingScore(_StrictModel):
    """The composite 0-100 score + plain-English verdict.

    0 == looks robust; 100 == almost certainly overfit.

    Calibration is v1 — see workers/overfitting/composite.py for the
    interpolation table. Phase 5 will recalibrate from accumulated
    backtest data.
    """

    score: float = Field(ge=0.0, le=100.0)
    verdict: OverfittingVerdict
    contributions: list[SignalContribution]
    explanation: str = Field(min_length=1, max_length=4000)
    confidence_band_low: float = Field(ge=0.0, le=100.0)
    confidence_band_high: float = Field(ge=0.0, le=100.0)


# ---- Top-level Analysis ---------------------------------------------------


class OverfittingAnalysis(_StrictModel):
    """The full bundle of four analyses + the composite score.

    Persisted as five separate JSONB columns so downstream queries
    can hit any one sub-result without parsing the whole blob, but
    written and read atomically as one object through this model.
    """

    schema_version: Literal["1.0"] = "1.0"
    walk_forward: WalkForwardResult
    parameter_sweep: ParameterSweepResult
    monte_carlo: MonteCarloResult
    deflated_sharpe: DeflatedSharpeResult
    composite: OverfittingScore
    compute_seconds: float = Field(ge=0.0)


__all__ = [
    "DeflatedSharpeResult",
    "MonteCarloHistogramBin",
    "MonteCarloResult",
    "OverfittingAnalysis",
    "OverfittingScore",
    "OverfittingVerdict",
    "ParameterSweepResult",
    "SignalContribution",
    "SweepAxis",
    "SweepAxisKind",
    "SweepCell",
    "WalkForwardResult",
    "WindowResult",
]
