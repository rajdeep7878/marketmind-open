"""Deflated Sharpe Ratio (Bailey & López de Prado 2014).

Reference: Bailey, D. H., & López de Prado, M. (2014).
"The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
Overfitting, and Non-Normality." Journal of Portfolio Management,
Spring 2014.

Implements the full formulation:

  1. Expected maximum Sharpe over N i.i.d. trials, assuming trial
     Sharpes ~ N(0, 1):

       E[max SR | N] = (1 - γ) * Φ⁻¹(1 - 1/N) + γ * Φ⁻¹(1 - 1/(N·e))

     where γ is the Euler-Mascheroni constant ≈ 0.5772.

  2. The Probabilistic Sharpe Ratio with the deflated benchmark:

       DSR = Φ(
           (SR_hat - E[max SR]) · sqrt(T - 1) /
           sqrt(1 - γ₃ · SR_hat + ((γ₄ - 1) / 4) · SR_hat²)
       )

     where:
       - SR_hat is the observed Sharpe
       - T is the number of return observations
       - γ₃ is returns skewness (Fisher; 0 for normal)
       - γ₄ is returns kurtosis (Pearson; 3 for normal — NOT excess)

`probability_strategy_is_real` is DSR — the probability that the true
underlying Sharpe is greater than the expected maximum of N i.i.d.
trial Sharpes, given the observed sample.

Assumption baked in: σ_SR (the std of the trial Sharpes) = 1. We
don't have an empirical distribution of "all the strategies someone
tried before showing us this one," so we normalise to unit variance.
This is what every public implementation of DSR does in practice.

`method == "haircut_v1"` is a simpler fallback for when we want a
quick, transparent number rather than the full formula: it returns
`SR_hat * N⁻¹ᐟ⁴` as the deflated value (the so-called Harvey-Liu
haircut). Currently unused in production; kept for reference and
benchmarking.
"""

from __future__ import annotations

import math
from typing import Final, Literal

import scipy.stats as stats
import structlog
from marketmind_shared.schemas import DeflatedSharpeResult

log = structlog.get_logger(__name__)


_EULER_MASCHERONI: Final[float] = 0.5772156649015329


def deflated_sharpe(
    observed_sharpe: float,
    *,
    n_trials_estimate: int = 100,
    n_observations: int = 252,
    returns_skewness: float = 0.0,
    returns_kurtosis: float = 3.0,
    method: Literal["lopez_de_prado_full", "haircut_v1"] = "lopez_de_prado_full",
) -> DeflatedSharpeResult:
    """Compute the deflated Sharpe + the probability the strategy is real.

    Defaults are calibrated for retail crypto strategies:

      - `n_trials_estimate = 100` (one author trying 100 variations
        before reporting their winner is conservative).
      - `n_observations = 252` — one year of daily bars. The caller
        usually passes the real bar count from the backtest.
      - `returns_skewness = 0`, `returns_kurtosis = 3` — assume normal
        returns by default. Caller passes empirical estimates when
        available.
    """
    if n_trials_estimate < 1:
        raise ValueError(f"n_trials_estimate must be >= 1; got {n_trials_estimate}")
    if n_observations < 2:
        raise ValueError(f"n_observations must be >= 2; got {n_observations}")

    if method == "haircut_v1":
        return _haircut_v1(
            observed_sharpe=observed_sharpe,
            n_trials_estimate=n_trials_estimate,
            n_observations=n_observations,
            returns_skewness=returns_skewness,
            returns_kurtosis=returns_kurtosis,
        )

    return _full_lopez_de_prado(
        observed_sharpe=observed_sharpe,
        n_trials_estimate=n_trials_estimate,
        n_observations=n_observations,
        returns_skewness=returns_skewness,
        returns_kurtosis=returns_kurtosis,
    )


def _full_lopez_de_prado(
    *,
    observed_sharpe: float,
    n_trials_estimate: int,
    n_observations: int,
    returns_skewness: float,
    returns_kurtosis: float,
) -> DeflatedSharpeResult:
    expected_max_sr = _expected_max_sharpe(n_trials_estimate)
    # Variance correction inside the PSR sqrt. The kurtosis term uses
    # γ₄ - 1 (NOT γ₄ - 3 — this is full Pearson kurtosis, where a
    # normal distribution has γ₄ = 3, so the term reduces to 0.5 *
    # SR², matching the Jobson-Korkie variance under normality).
    denom_var = (
        1.0
        - returns_skewness * observed_sharpe
        + ((returns_kurtosis - 1.0) / 4.0) * observed_sharpe**2
    )
    # Floor for numerical stability — very negative skew can drive
    # this below zero for extreme SR values.
    denom_var = max(denom_var, 1e-9)
    z_numerator = (observed_sharpe - expected_max_sr) * math.sqrt(n_observations - 1)
    z = z_numerator / math.sqrt(denom_var)
    probability = float(stats.norm.cdf(z))

    return DeflatedSharpeResult(
        observed_sharpe=observed_sharpe,
        deflated_sharpe_ratio=observed_sharpe - expected_max_sr,
        probability_strategy_is_real=_clamp01(probability),
        n_trials_estimate=n_trials_estimate,
        n_observations=n_observations,
        returns_skewness=returns_skewness,
        returns_kurtosis=returns_kurtosis,
        expected_max_sharpe=expected_max_sr,
        method="lopez_de_prado_full",
    )


def _haircut_v1(
    *,
    observed_sharpe: float,
    n_trials_estimate: int,
    n_observations: int,
    returns_skewness: float,
    returns_kurtosis: float,
) -> DeflatedSharpeResult:
    """Harvey-Liu-style haircut. SR * N^(-1/4) is a crude shrinkage
    that ignores the return distribution. Kept for benchmarking the
    full formula; do NOT ship.
    """
    haircut = observed_sharpe * (n_trials_estimate**-0.25)
    # Map to a probability via the standard normal CDF on the haircut
    # value scaled by sqrt(T-1) (the same scaling the full formula
    # would apply if returns were normal and N=1).
    z = haircut * math.sqrt(n_observations - 1)
    probability = float(stats.norm.cdf(z))
    return DeflatedSharpeResult(
        observed_sharpe=observed_sharpe,
        deflated_sharpe_ratio=haircut,
        probability_strategy_is_real=_clamp01(probability),
        n_trials_estimate=n_trials_estimate,
        n_observations=n_observations,
        returns_skewness=returns_skewness,
        returns_kurtosis=returns_kurtosis,
        expected_max_sharpe=observed_sharpe - haircut,
        method="haircut_v1",
    )


def _expected_max_sharpe(n_trials: int) -> float:
    """Bailey-LdP approximation to E[max Sharpe | N i.i.d. trials, σ=1]."""
    n = max(2, n_trials)
    term_a = (1.0 - _EULER_MASCHERONI) * float(stats.norm.ppf(1.0 - 1.0 / n))
    term_b = _EULER_MASCHERONI * float(stats.norm.ppf(1.0 - 1.0 / (n * math.e)))
    return term_a + term_b


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


__all__ = ["deflated_sharpe"]
