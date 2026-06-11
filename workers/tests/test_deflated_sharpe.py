"""Tests for the Deflated Sharpe Ratio.

We don't have access to López de Prado's exact published numerical
examples; the tests below cover the *qualitative* behaviour of the
formula and one *quantitative* sanity check against a hand-computed
value for known inputs.

Boundary cases checked:
  - More trials → lower probability_strategy_is_real (deflation works).
  - Larger sample → higher probability_strategy_is_real (data shrinks
    standard error).
  - Negative skewness penalises (downside-heavy distributions are
    worse than the Sharpe alone suggests).
  - Higher kurtosis penalises (fat tails inflate Sharpe noise).
  - probability is in [0, 1].
"""

from __future__ import annotations

import math

import pytest
from marketmind_workers.overfitting.deflated_sharpe import (
    _expected_max_sharpe,  # type: ignore[attr-defined]
    deflated_sharpe,
)

# ---- E[max Sharpe] sanity ------------------------------------------------


def test_expected_max_sharpe_increases_with_trials() -> None:
    """E[max SR] grows monotonically with the number of trials."""
    e1 = _expected_max_sharpe(2)
    e10 = _expected_max_sharpe(10)
    e100 = _expected_max_sharpe(100)
    e1000 = _expected_max_sharpe(1000)
    assert e1 < e10 < e100 < e1000


def test_expected_max_sharpe_known_value_for_100_trials() -> None:
    """For N=100, σ_SR=1:

        Φ⁻¹(0.99) ≈ 2.3263
        Φ⁻¹(1 - 1/(100*e)) = Φ⁻¹(0.99632) ≈ 2.6824
        E[max SR] = (1 - 0.5772) * 2.3263 + 0.5772 * 2.6824
                  ≈ 0.4228 * 2.3263 + 0.5772 * 2.6824
                  ≈ 0.9834 + 1.5482
                  ≈ 2.5316

    Hand-computed reference value. We allow a 0.5% tolerance because
    scipy's norm.ppf is slightly different from the table approximation.
    """
    assert _expected_max_sharpe(100) == pytest.approx(2.5316, rel=0.005)


# ---- DSR behavioural checks ---------------------------------------------


def test_high_sharpe_with_few_trials_high_probability() -> None:
    """SR=3.0, N=1 (no fishing), long sample → near 1.0 probability."""
    out = deflated_sharpe(
        3.0,
        n_trials_estimate=1,
        n_observations=2000,
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )
    assert out.probability_strategy_is_real > 0.99


def test_moderate_sharpe_with_many_trials_low_probability() -> None:
    """SR=1.5, N=1000. E[max SR | 1000] ≈ 3.0 — well above 1.5 — so
    the DSR probability should collapse.
    """
    out = deflated_sharpe(
        1.5,
        n_trials_estimate=1000,
        n_observations=2000,
        returns_skewness=0.0,
        returns_kurtosis=3.0,
    )
    assert out.probability_strategy_is_real < 0.05


def test_more_trials_strictly_reduces_probability() -> None:
    """Holding everything else fixed, more trials → lower probability."""
    args = {"n_observations": 1000, "returns_skewness": 0.0, "returns_kurtosis": 3.0}
    out_10 = deflated_sharpe(2.0, n_trials_estimate=10, **args)
    out_100 = deflated_sharpe(2.0, n_trials_estimate=100, **args)
    out_1000 = deflated_sharpe(2.0, n_trials_estimate=1000, **args)
    assert (
        out_10.probability_strategy_is_real
        > out_100.probability_strategy_is_real
        > out_1000.probability_strategy_is_real
    )


def test_more_observations_strictly_increases_probability() -> None:
    """For a given (SR, N), more observations tighten the standard
    error → higher probability the SR is genuine.
    """
    args = {"n_trials_estimate": 100, "returns_skewness": 0.0, "returns_kurtosis": 3.0}
    out_100 = deflated_sharpe(3.0, n_observations=100, **args)
    out_1000 = deflated_sharpe(3.0, n_observations=1000, **args)
    out_5000 = deflated_sharpe(3.0, n_observations=5000, **args)
    assert (
        out_100.probability_strategy_is_real
        < out_1000.probability_strategy_is_real
        < out_5000.probability_strategy_is_real
    )


def test_negative_skewness_reduces_probability() -> None:
    """Holding SR fixed, more negative skewness → lower probability.

    Variance term: 1 - γ₃·SR_hat + ... → bigger when γ₃ < 0 →
    bigger denominator → smaller z → smaller CDF.
    """
    common = {
        "n_trials_estimate": 50,
        "n_observations": 1000,
        "returns_kurtosis": 3.0,
    }
    out_pos = deflated_sharpe(2.5, returns_skewness=0.5, **common)
    out_zero = deflated_sharpe(2.5, returns_skewness=0.0, **common)
    out_neg = deflated_sharpe(2.5, returns_skewness=-0.5, **common)
    assert (
        out_pos.probability_strategy_is_real
        > out_zero.probability_strategy_is_real
        > out_neg.probability_strategy_is_real
    )


def test_higher_kurtosis_reduces_probability() -> None:
    """Fat-tailed returns inflate the Sharpe's standard error."""
    common = {
        "n_trials_estimate": 50,
        "n_observations": 1000,
        "returns_skewness": 0.0,
    }
    out_normal = deflated_sharpe(2.5, returns_kurtosis=3.0, **common)
    out_fat = deflated_sharpe(2.5, returns_kurtosis=12.0, **common)
    assert out_normal.probability_strategy_is_real > out_fat.probability_strategy_is_real


def test_probability_in_unit_interval() -> None:
    """Boundary check across a sweep of inputs."""
    for sr in (-5.0, -1.0, 0.0, 1.0, 5.0):
        out = deflated_sharpe(
            sr,
            n_trials_estimate=100,
            n_observations=500,
            returns_skewness=0.0,
            returns_kurtosis=3.0,
        )
        assert 0.0 <= out.probability_strategy_is_real <= 1.0


# ---- Haircut method ------------------------------------------------------


def test_haircut_method_returns_known_shape() -> None:
    """Haircut formula: deflated_sharpe = SR * N^(-1/4).
    For SR=2.0, N=16 → haircut = 2.0 * 16^(-0.25) = 2.0 * 0.5 = 1.0.
    """
    out = deflated_sharpe(
        2.0,
        n_trials_estimate=16,
        n_observations=1000,
        method="haircut_v1",
    )
    assert out.deflated_sharpe_ratio == pytest.approx(1.0)
    assert out.method == "haircut_v1"


# ---- Bad-arg guards ------------------------------------------------------


def test_rejects_too_few_observations() -> None:
    with pytest.raises(ValueError, match="n_observations"):
        deflated_sharpe(1.0, n_trials_estimate=10, n_observations=1)


def test_rejects_zero_trials() -> None:
    with pytest.raises(ValueError, match="n_trials_estimate"):
        deflated_sharpe(1.0, n_trials_estimate=0, n_observations=100)


_ = math  # silence unused import
