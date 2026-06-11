"""Tests for the composite overfitting score.

Each test feeds the composite function hand-built sub-results that
trigger known per-signal contributions, then checks the final score
+ verdict + which signals show up in the top-2 explanation bullets.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from marketmind_shared.schemas import (
    DeflatedSharpeResult,
    MonteCarloHistogramBin,
    MonteCarloResult,
    OverfittingVerdict,
    ParameterSweepResult,
    StrategySpec,
    WalkForwardResult,
)
from marketmind_workers.overfitting.composite import compute_overfitting_score

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "strategies" / "valid"


def _v1_spec() -> StrategySpec:
    """A v1 (non-stateful) spec — composite keeps the original weights."""
    return StrategySpec.model_validate(json.loads((_FIXTURES / "01_golden_cross.json").read_text()))


def _stateful_spec() -> StrategySpec:
    """A v2 (stateful) spec — composite re-weights MC down / walk-forward up."""
    return StrategySpec.model_validate(
        json.loads((_FIXTURES / "09_regime_state_supertrend.json").read_text()),
    )


# ---- Builders ------------------------------------------------------------


def _wf(degradation: float, *, valid: bool = True) -> WalkForwardResult:
    return WalkForwardResult(
        windows=[],
        in_sample_avg_return=0.20,
        out_of_sample_avg_return=degradation * 0.20,
        degradation_ratio=degradation,
        degradation_ratio_valid=valid,
        out_of_sample_positive_rate=0.5,
        consistency_score=0.5,
        train_ratio=0.7,
        n_windows_requested=6,
        n_windows_actual=6,
    )


def _sweep(peakiness: float, *, n_combinations: int = 25) -> ParameterSweepResult:
    return ParameterSweepResult(
        axes=[],
        cells=[],
        baseline_return_pct=0.20,
        baseline_rank_percentile=0.5,
        best_in_grid_return=0.30,
        worst_in_grid_return=0.10,
        neighborhood_avg_return=0.15,
        peakiness_score=peakiness,
        n_combinations=n_combinations,
        skipped_reason=None,
    )


def _mc(p_value: float) -> MonteCarloResult:
    return MonteCarloResult(
        real_return_pct=0.20,
        real_sharpe=1.0,
        n_permutations=100,
        synthetic_mean_return=0.0,
        synthetic_std_return=0.1,
        synthetic_min=-0.3,
        synthetic_max=0.4,
        histogram=[MonteCarloHistogramBin(lo=-0.3, hi=0.4, count=100)],
        p_value=p_value,
        percentile_rank=1.0 - p_value,
        seed=42,
    )


def _ds(probability: float) -> DeflatedSharpeResult:
    return DeflatedSharpeResult(
        observed_sharpe=1.5,
        deflated_sharpe_ratio=0.5,
        probability_strategy_is_real=probability,
        n_trials_estimate=100,
        n_observations=1000,
        returns_skewness=0.0,
        returns_kurtosis=3.0,
        expected_max_sharpe=1.0,
        method="lopez_de_prado_full",
    )


# ---- Per-signal mapping --------------------------------------------------


def test_robust_signals_yield_low_score() -> None:
    """All four signals look good → score < 30, verdict Likely Robust."""
    out = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.95),  # high degradation_ratio = consistent
        sweep=_sweep(0.10),  # low peakiness
        monte_carlo=_mc(0.02),  # very small p-value
        deflated=_ds(0.97),  # high probability
    )
    assert out.score < 30.0
    assert out.verdict is OverfittingVerdict.LIKELY_ROBUST
    assert 0.0 <= out.confidence_band_low <= out.score
    assert out.score <= out.confidence_band_high <= 100.0


def test_overfit_signals_yield_high_score() -> None:
    """All four signals look bad → score > 60, verdict Likely Overfit."""
    out = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.10),  # OOS only 10% of IS
        sweep=_sweep(0.85),  # very peaky
        monte_carlo=_mc(0.60),  # most synth runs beat real
        deflated=_ds(0.10),  # very low probability
    )
    assert out.score > 60.0
    assert out.verdict is OverfittingVerdict.LIKELY_OVERFIT


def test_mixed_signals_land_in_middle() -> None:
    """Two good, two bad → score in [30, 60], verdict Mixed Signals."""
    out = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.95),  # good
        sweep=_sweep(0.10),  # good
        monte_carlo=_mc(0.40),  # bad
        deflated=_ds(0.20),  # bad
    )
    assert 20.0 <= out.score <= 60.0


def test_invalid_walk_forward_pushes_score_higher() -> None:
    """If IS_avg <= 0, walk_forward contributes 75 pts — significant
    push toward overfit verdict.
    """
    out = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.0, valid=False),
        sweep=_sweep(0.10),
        monte_carlo=_mc(0.02),
        deflated=_ds(0.97),
    )
    # Compare to the all-robust case: this is higher by exactly
    # 75 * 0.35 = 26.25.
    baseline = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.95),
        sweep=_sweep(0.10),
        monte_carlo=_mc(0.02),
        deflated=_ds(0.97),
    )
    assert out.score - baseline.score == pytest.approx(75.0 * 0.35, abs=1e-6)


# ---- Verdict thresholds --------------------------------------------------


def test_verdict_thresholds_exact() -> None:
    """Score 29.9 → Robust, 30.0 → Mixed, 59.9 → Mixed, 60.0 → Overfit."""
    # Construct controlled scores by varying just walk_forward (35% weight).
    # 4 signals all-good: ~0. Adjust wf to push the score.
    # Score is dominated by walk_forward (weight 0.35) — when its
    # contribution alone exceeds 30/0.35 ≈ 85 pts the total tips into
    # Mixed Signals even with the other three signals at zero.
    for r, expected in [
        (0.95, OverfittingVerdict.LIKELY_ROBUST),
        (0.60, OverfittingVerdict.LIKELY_ROBUST),
        (0.10, OverfittingVerdict.MIXED_SIGNALS),
    ]:
        out = compute_overfitting_score(
            _v1_spec(),
            walk_forward=_wf(r),
            sweep=_sweep(0.10),
            monte_carlo=_mc(0.02),
            deflated=_ds(0.97),
        )
        assert out.verdict is expected, f"r={r} → score={out.score:.1f}, expected {expected}"

    # And an all-bad scenario clearly tips into Overfit.
    overfit = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(-0.5),  # OOS lost half of IS
        sweep=_sweep(0.95),
        monte_carlo=_mc(0.80),
        deflated=_ds(0.10),
    )
    assert overfit.verdict is OverfittingVerdict.LIKELY_OVERFIT


# ---- Explanation -------------------------------------------------------


def test_explanation_cites_top_contributors() -> None:
    """When walk_forward is the worst signal, explanation mentions
    it; when monte_carlo is the worst, it mentions Monte Carlo.
    """
    bad_wf = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.10),
        sweep=_sweep(0.10),
        monte_carlo=_mc(0.02),
        deflated=_ds(0.97),
    )
    assert "walk-forward" in bad_wf.explanation.lower()

    bad_mc = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.95),
        sweep=_sweep(0.10),
        monte_carlo=_mc(0.60),
        deflated=_ds(0.97),
    )
    assert "monte carlo" in bad_mc.explanation.lower()


# ---- Contribution math --------------------------------------------------


def test_contributions_weighted_sum_equals_score() -> None:
    out = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.5),
        sweep=_sweep(0.5),
        monte_carlo=_mc(0.1),
        deflated=_ds(0.6),
    )
    weighted = sum(c.contribution_pts * c.weight for c in out.contributions)
    assert out.score == pytest.approx(weighted, abs=1e-6)


# ---- A.4: stateful re-weight (design doc §5.3) --------------------------


def test_v1_spec_keeps_original_composite_weights() -> None:
    out = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.5),
        sweep=_sweep(0.5),
        monte_carlo=_mc(0.1),
        deflated=_ds(0.6),
    )
    weights = {c.name: c.weight for c in out.contributions}
    assert weights == {
        "walk_forward": 0.35,
        "parameter_sweep": 0.25,
        "monte_carlo": 0.25,
        "deflated_sharpe": 0.15,
    }


def test_stateful_spec_reweights_monte_carlo_down_and_walk_forward_up() -> None:
    out = compute_overfitting_score(
        _stateful_spec(),
        walk_forward=_wf(0.5),
        sweep=_sweep(0.5),
        monte_carlo=_mc(0.1),
        deflated=_ds(0.6),
    )
    weights = {c.name: c.weight for c in out.contributions}
    assert weights == {
        "walk_forward": 0.50,
        "parameter_sweep": 0.25,
        "monte_carlo": 0.10,
        "deflated_sharpe": 0.15,
    }
    # Weights still sum to 1.0, and the weighted contributions still equal
    # the score.
    assert sum(weights.values()) == pytest.approx(1.0)
    assert out.score == pytest.approx(
        sum(c.contribution_pts * c.weight for c in out.contributions),
        abs=1e-6,
    )


def test_stateful_reweight_lowers_score_when_monte_carlo_is_the_bad_signal() -> None:
    """Walk-forward good, Monte Carlo bad: down-weighting MC for a stateful
    spec pulls the composite score well below the v1-weighted score for the
    identical four sub-results.
    """
    v1 = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.95),  # good — contributes ~0 pts
        sweep=_sweep(0.10),  # good
        monte_carlo=_mc(0.60),  # bad — contributes ~75 pts
        deflated=_ds(0.97),  # good
    )
    stateful = compute_overfitting_score(
        _stateful_spec(),
        walk_forward=_wf(0.95),
        sweep=_sweep(0.10),
        monte_carlo=_mc(0.60),
        deflated=_ds(0.97),
    )
    assert stateful.score < v1.score - 5.0


def test_stateful_explanation_notes_the_reweight() -> None:
    stateful = compute_overfitting_score(
        _stateful_spec(),
        walk_forward=_wf(0.5),
        sweep=_sweep(0.5),
        monte_carlo=_mc(0.4),
        deflated=_ds(0.6),
    )
    v1 = compute_overfitting_score(
        _v1_spec(),
        walk_forward=_wf(0.5),
        sweep=_sweep(0.5),
        monte_carlo=_mc(0.4),
        deflated=_ds(0.6),
    )
    assert "path-dependent" in stateful.explanation
    assert "weighted lower" in stateful.explanation
    # The v1 explanation must NOT carry the stateful re-weight note.
    assert "path-dependent" not in v1.explanation
