"""Phase 3.2 backtest metrics, benchmark, and author-claim comparison.

These are the structured outputs Phase 3.2 produces on top of a Phase
3.1 BacktestRun. The split:

  BacktestMetrics  — annualised performance + trade stats derived from
                     the run's equity curve + trade list. No engine
                     coupling — a Phase 4 walk-forward / Monte-Carlo
                     pass can compute these on a sub-window without
                     re-running the engine.

  BenchmarkResult  — buy-and-hold of the same instrument over the same
                     date range, with the same commission applied.
                     Carries its own equity curve so the UI can plot
                     strategy-vs-B&H on shared axes.

  BenchmarkComparison — alpha + risk-adjusted comparison + an honest
                        plain-English verdict.

  AuthorClaimComparison — per-claim: parsed author value, measured
                          value, discrepancy ratio, and an explanation
                          that flags known structural divergences
                          (multi-asset, in-sample optimisation, etc.).

  BacktestResult — the top-level row a backtest job writes. Holds
                   a snapshot of the spec, the run, all of the above,
                   plus observability fields (timings).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.backtest_run import BacktestRun
from marketmind_shared.schemas.extraction_report.rules import AuthorClaimType
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.strategy_spec.spec import StrategySpec


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


# ---- BacktestMetrics --------------------------------------------------------


class BacktestMetrics(_StrictModel):
    """Annualised performance + trade-level statistics.

    All percent fields are stored as fractions (0.12 == 12%) so chart
    code can scale them uniformly. Annualization always uses the
    actual timeframe's bars-per-year — a 4h backtest does NOT use
    daily-bar annualization.

    Edge-case handling:
      - `profit_factor` is `infinity` when there are zero losing trades
        (and at least one winning trade). Capped at 1e6 so JSON
        survives, with `profit_factor_capped` flagged so the UI can
        annotate.
      - With zero trades, all trade-level fields are 0 / 0.0 / "".
      - With one trade, streak fields == 1 if winning, 0 if losing.
    """

    # Return-side
    total_return_pct: float
    cagr: float
    annualized_volatility: float
    sharpe_ratio: float
    sortino_ratio: float

    # Drawdown
    max_drawdown_pct: float = Field(ge=0.0)
    max_drawdown_duration_days: int = Field(ge=0)
    calmar_ratio: float

    # Trade-level
    num_trades: int = Field(ge=0)
    win_rate: float = Field(ge=0.0, le=1.0)
    profit_factor: float = Field(ge=0.0)
    profit_factor_capped: bool = False
    avg_win_pct: float
    avg_loss_pct: float
    expectancy: float
    largest_win_pct: float
    largest_loss_pct: float
    longest_winning_streak: int = Field(ge=0)
    longest_losing_streak: int = Field(ge=0)
    avg_trade_duration_days: float = Field(ge=0.0)
    exposure_pct: float = Field(ge=0.0, le=1.0)

    # Observability
    bars_processed: int = Field(ge=0)
    bars_per_year: float = Field(gt=0.0)


# ---- BenchmarkResult --------------------------------------------------------


class BenchmarkEquityPoint(_StrictModel):
    """One point on the benchmark equity curve.

    Separate from BacktestRun's EquityPoint so callers can't
    accidentally splice the two: the benchmark curve has different
    semantics (it's a passive hold, not a trade-driven value).
    """

    timestamp: datetime
    value: float

    @field_validator("timestamp")
    @classmethod
    def _ts_utc(cls, v: datetime) -> datetime:
        return _require_utc("benchmark timestamp", v)


class BenchmarkResult(_StrictModel):
    """Buy-and-hold over the same instrument + date range as the strategy."""

    total_return_pct: float
    cagr: float
    max_drawdown_pct: float = Field(ge=0.0)
    sharpe_ratio: float
    final_value: float
    initial_value: float = Field(gt=0.0)
    equity_curve: list[BenchmarkEquityPoint]


# ---- BenchmarkComparison ----------------------------------------------------


class BenchmarkComparison(_StrictModel):
    """Strategy-vs-B&H comparison with an honest verdict string.

    `alpha_pct` is strategy_return - benchmark_return (fraction).
    `risk_adjusted_alpha` is strategy_sharpe - benchmark_sharpe.
    `verdict` is the single-paragraph plain-English summary intended
    to be the headline finding on the results page.
    """

    strategy_return_pct: float
    benchmark_return_pct: float
    alpha_pct: float
    beat_benchmark: bool
    strategy_sharpe: float
    benchmark_sharpe: float
    risk_adjusted_alpha: float
    verdict: str = Field(min_length=1, max_length=2000)


# ---- AuthorClaimComparison --------------------------------------------------


class AuthorClaimComparison(_StrictModel):
    """One row of the author-vs-measured table.

    `author_value_parsed` is null when we couldn't parse the author's
    string into a number — surface as informational ("author said X
    but we couldn't extract a number to compare").

    `discrepancy_ratio` = (measured - author) / author when both are
    non-zero and we measured the same metric. Null when not applicable.
    """

    claim_type: AuthorClaimType
    author_value_raw: str = Field(min_length=1, max_length=200)
    author_value_parsed: float | None = None
    measured_value: float | None = None
    measured_label: str = Field(default="", max_length=64)
    difference: float | None = None
    discrepancy_ratio: float | None = None
    explanation: str = Field(min_length=1, max_length=2000)


# ---- BacktestResult ---------------------------------------------------------


class BacktestResult(_StrictModel):
    """The top-level row a backtest job writes.

    Carries:
      - spec_snapshot: the StrategySpec exactly as backtested (for
        reproducibility — if Phase 1 schema evolves, this row still
        reflects what we ran)
      - run: the full BacktestRun (equity curve at full resolution +
        trades). The list endpoint downsamples this for transport.
      - metrics / benchmark / benchmark_comparison /
        author_claim_comparisons: the four analyses
      - timings: data_fetch_seconds and compute_seconds so we can
        budget Phase 4's walk-forward expansion sensibly

    schema_version is Literal["1.0"] so a future structural change
    forces a new row type rather than silently breaking consumers.
    """

    schema_version: Literal["1.0"] = "1.0"
    spec_snapshot: StrategySpec
    run: BacktestRun
    metrics: BacktestMetrics
    benchmark: BenchmarkResult
    benchmark_comparison: BenchmarkComparison
    author_claim_comparisons: list[AuthorClaimComparison] = Field(default_factory=list)

    data_fetch_seconds: float = Field(ge=0.0)
    compute_seconds: float = Field(ge=0.0)


__all__ = [
    "AuthorClaimComparison",
    "BacktestMetrics",
    "BacktestResult",
    "BenchmarkComparison",
    "BenchmarkEquityPoint",
    "BenchmarkResult",
]
