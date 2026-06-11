"""G1-G9 PASS gates + verdict vocabulary (mandate Stage 4).

All gates evaluate on stitched OOS results, per (strategy x venue profile).
Deploy-eligible for paper = PASS on >= 1 profile with
uk_execution_feasible=True. A strategy passing only on
binance_spot_reference is CONDITIONAL_PASS_INFEASIBLE_VENUE: research-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from marketmind_workers.ftr.validation.metrics import NetMetrics


class Verdict(StrEnum):
    PASS = "PASS"  # noqa: S105 — verdict label, not a credential
    PASS_LOW_FREQUENCY = "PASS_LOW_FREQUENCY"  # noqa: S105
    CONDITIONAL_PASS_INFEASIBLE_VENUE = "CONDITIONAL_PASS_INFEASIBLE_VENUE"  # noqa: S105
    REJECTED = "REJECTED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


# Gate thresholds — mandate Stage 4. The repo gauntlet has no stricter
# Sharpe floor than 0.8 (verified Stage 0), so G2 stands at 0.8.
G1_MIN_PROFIT_FACTOR = 1.15
G2_MIN_SHARPE = 0.8
G3_MIN_DSR_PROB = 0.95
G4_MIN_POSITIVE_FOLD_FRAC = 0.60
G5_MIN_RANDOM_PERCENTILE = 0.95
G6_MAX_COST_OVER_EDGE = 0.5
G7_MIN_PLATEAU_RATIO = 0.70
G9_FREQ_MIN_PER_DAY = 0.2
G9_FREQ_MAX_PER_DAY = 5.0


@dataclass
class GateInputs:
    metrics: NetMetrics
    dsr_probability: float | None
    positive_fold_fraction: float | None
    random_entry_percentile: float | None  # fraction of sims beaten by real
    plateau_ratio: float | None  # median neighbor metric / chosen cell
    expectancy_at_1p5x_cost: float | None
    n_trials: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class GateReport:
    strategy_id: str
    venue_profile: str
    uk_execution_feasible: bool
    verdict: Verdict
    failed_gates: list[str]
    passed_gates: list[str]
    skipped_gates: list[str]
    n_trials: int
    metrics: dict[str, float | int]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "venue_profile": self.venue_profile,
            "uk_execution_feasible": self.uk_execution_feasible,
            "verdict": str(self.verdict),
            "failed_gates": self.failed_gates,
            "passed_gates": self.passed_gates,
            "skipped_gates": self.skipped_gates,
            "n_trials": self.n_trials,
            "metrics": self.metrics,
            "notes": self.notes,
        }


def evaluate_gates(
    *,
    strategy_id: str,
    venue_profile: str,
    uk_execution_feasible: bool,
    inputs: GateInputs,
) -> GateReport:
    m = inputs.metrics
    failed: list[str] = []
    passed: list[str] = []
    skipped: list[str] = []

    def check(name: str, ok: bool | None) -> None:
        if ok is None:
            skipped.append(name)
        elif ok:
            passed.append(name)
        else:
            failed.append(name)

    if m.num_trades == 0:
        # No OOS trades at all: the cost gate suppressed everything. That
        # is a REJECTED-for-deployment outcome (nothing to deploy), reported
        # without statistical decoration.
        return GateReport(
            strategy_id=strategy_id,
            venue_profile=venue_profile,
            uk_execution_feasible=uk_execution_feasible,
            verdict=Verdict.REJECTED,
            failed_gates=["G9_frequency_zero_trades"],
            passed_gates=[],
            skipped_gates=["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"],
            n_trials=inputs.n_trials,
            metrics=m.to_dict(),
            notes=[*inputs.notes, "zero OOS trades — EV/cost gate suppressed all entries"],
        )

    check("G1_expectancy_pf", m.expectancy > 0 and m.profit_factor >= G1_MIN_PROFIT_FACTOR)
    check("G2_sharpe", m.sharpe >= G2_MIN_SHARPE)
    check(
        "G3_dsr",
        None if inputs.dsr_probability is None else inputs.dsr_probability >= G3_MIN_DSR_PROB,
    )
    check(
        "G4_folds_positive",
        None
        if inputs.positive_fold_fraction is None
        else inputs.positive_fold_fraction >= G4_MIN_POSITIVE_FOLD_FRAC,
    )
    check(
        "G5_beats_random",
        None
        if inputs.random_entry_percentile is None
        else inputs.random_entry_percentile >= G5_MIN_RANDOM_PERCENTILE,
    )
    check("G6_cost_over_edge", m.cost_over_gross_edge <= G6_MAX_COST_OVER_EDGE)
    check(
        "G7_plateau",
        None if inputs.plateau_ratio is None else inputs.plateau_ratio >= G7_MIN_PLATEAU_RATIO,
    )
    check(
        "G8_cost_sensitivity",
        None
        if inputs.expectancy_at_1p5x_cost is None
        else inputs.expectancy_at_1p5x_cost > 0,
    )

    freq = m.trades_per_day
    low_freq = freq < G9_FREQ_MIN_PER_DAY
    high_freq = freq > G9_FREQ_MAX_PER_DAY
    if high_freq:
        failed.append("G9_frequency_above_band")
    elif not low_freq:
        passed.append("G9_frequency_in_band")
    # below band handled in verdict (PASS_LOW_FREQUENCY), not a hard fail

    if failed:
        verdict = Verdict.REJECTED
    elif low_freq:
        verdict = Verdict.PASS_LOW_FREQUENCY
        passed.append("G9_frequency_below_band_archived")
    elif not uk_execution_feasible:
        verdict = Verdict.CONDITIONAL_PASS_INFEASIBLE_VENUE
    else:
        verdict = Verdict.PASS

    return GateReport(
        strategy_id=strategy_id,
        venue_profile=venue_profile,
        uk_execution_feasible=uk_execution_feasible,
        verdict=verdict,
        failed_gates=failed,
        passed_gates=passed,
        skipped_gates=skipped,
        n_trials=inputs.n_trials,
        metrics=m.to_dict(),
        notes=inputs.notes,
    )
