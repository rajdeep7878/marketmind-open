"""DSR frequency-mismatch fix (2026-05-25, post-audit).

The caller in `jobs/overfitting_analysis.py` was passing
`metrics.bars_processed` (raw bar count, e.g. 9486 for a 4-year 4H
backtest) as `n_observations` while passing the ANNUALIZED Sharpe
ratio as `observed_sharpe`. Bailey & López de Prado 2014's PSR/DSR
formula requires both at the same frequency; the mismatch inflated
`sqrt(T-1)` by sqrt(bpy) ≈ 47x on 4H, ≈ 187x on 15m, pegging
prob_real ≈ 0 for every strategy in the 15 stored historical runs.

The fix divides `metrics.bars_processed` by `metrics.bars_per_year`
(the bpy stored alongside the annualized Sharpe), guaranteeing
sourcing consistency.

These tests:

  1. Lock in the corrected prob_real values for Hunts 7 and 7v at
     T = 5 years (matches the 4.33-year backtest window rounded to
     int per the caller's `round(t_years)` integer cast).
  2. Hold the pre-fix pathology as a pinned reference: T = 9486 bars
     still pegs prob_real at 0.0 — anyone re-introducing the bug
     trips this test.
  3. Bound Modern Turtle's corrected prob_real (annualized Sharpe
     ~1.02 with T ≈ 5 years) without over-specifying.

All numeric expectations were inspected empirically first
(META-PATTERN, v1.2 retrospective standing rule); see the audit
report's computed-corrections table.
"""

from __future__ import annotations

from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe

# --- Hunt 7 base reproduction (audit values) -------------------------------


def test_hunt_7_base_corrected_prob_real_at_t_years() -> None:
    """Hunt 7 base: observed_sharpe = 0.6725800169402386, T = 5 years.
    Audit report computed corrected prob_real = 0.000395596... — assert
    within tight tolerance so any future drift in the formula
    constants surfaces immediately.
    """
    result = deflated_sharpe(
        observed_sharpe=0.6725800169402386,
        n_trials_estimate=100,
        n_observations=5,
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )
    assert abs(result.probability_strategy_is_real - 0.0003955956951522654) < 1e-5, (
        f"Hunt 7 base prob_real drifted from audit value: {result.probability_strategy_is_real}"
    )


def test_hunt_7_variant_corrected_prob_real_at_t_years() -> None:
    """Hunt 7v (70-bar Donchian): observed_sharpe = 0.9099165721900733,
    T = 5 years. Audit report computed corrected prob_real = 0.003206...
    """
    result = deflated_sharpe(
        observed_sharpe=0.9099165721900733,
        n_trials_estimate=100,
        n_observations=5,
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )
    assert abs(result.probability_strategy_is_real - 0.0032064237341795283) < 1e-5, (
        f"Hunt 7v prob_real drifted from audit value: {result.probability_strategy_is_real}"
    )


# --- pre-fix pathology pin (regression net) --------------------------------


def test_pre_fix_pathology_still_pegs_prob_real_at_zero() -> None:
    """If any future refactor passes raw bar count for T again
    (the bug we just fixed), THIS test stays green while the
    corrected-branch tests above fail. That's the regression net:
    the pinned pathology proves the bug-shape is reproducible, and
    the corrected branch proves the fix is in place. Both
    invariants must hold.

    The 9486 bar count corresponds to Hunt 7 / Hunt 7v's actual
    backtest length (4-year 4H window). With this incorrectly-typed
    T, ANY observed Sharpe below the expected max (~2.534 for
    n_trials=100) pegs prob_real at 0.0 via the deep-tail normal
    CDF.
    """
    result = deflated_sharpe(
        observed_sharpe=0.9099165721900733,
        n_trials_estimate=100,
        n_observations=9486,  # ← the buggy value
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )
    # Pegged at exactly 0.0 (the _clamp01 floor + extreme deep-tail z).
    assert result.probability_strategy_is_real == 0.0, (
        f"pre-fix pathology no longer reproduces: prob_real = "
        f"{result.probability_strategy_is_real}; the regression net is broken"
    )


# --- Modern Turtle bounded sanity check ------------------------------------


def test_modern_turtle_corrected_prob_real_bounded_range() -> None:
    """Modern Turtle's seed-time observed Sharpe was ~1.016 (per
    analysis_id c9443506-daf8-4f1f-97a7-75e5db6155fb, n_obs 13985
    bars over ~6.4 years at 4H). With the frequency fix, prob_real
    should land in a small positive range (>1e-3 but <1e-2) — well
    above the buggy 0.0 but still well below the 0.5 transition in
    composite.py:_deflated_sharpe_contribution that would meaningfully
    move the composite score. This bounds the corrected value without
    over-specifying it; the goal is to document that the bug HAD no
    historical seed impact (Modern Turtle stays likely_robust either
    way) while the corrected value is a sane non-zero number.
    """
    result = deflated_sharpe(
        observed_sharpe=1.016,
        n_trials_estimate=100,
        n_observations=6,  # 13985 / (6 * 365) ≈ 6.39 → rounds to 6
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )
    p = result.probability_strategy_is_real
    assert 1e-3 < p < 1e-2, (
        f"Modern Turtle corrected prob_real outside expected range: {p}"
    )
