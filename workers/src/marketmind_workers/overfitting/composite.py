"""Composite overfitting score.

Takes the four independent analyses (walk-forward, parameter sweep,
Monte Carlo, deflated Sharpe) and folds them into a single 0-100
score where 0 == looks robust and 100 == almost certainly overfit.

Calibration is v1. The interpolation tables below are intentionally
linear and conservative — they'll be tuned in Phase 5 once we have
empirical scores from a population of strategies. The constants here
are documented so a Phase 5 recalibration script can pick them up.

Verdict thresholds:
  - score 0-30   → "Likely Robust"
  - score 30-60  → "Mixed Signals"
  - score 60-100 → "Likely Overfit"

Weights:
  - walk_forward   0.35  (most direct overfitting signal — does the
                          strategy generalise out-of-sample?)
  - parameter_sweep 0.25 (parameter-neighborhood structure — was the
                          spec chosen on the lone peak?)
  - monte_carlo    0.25 (does the edge survive when time-series
                          structure is destroyed?)
  - deflated_sharpe 0.15 (probability the Sharpe wasn't a max-over-
                          trials artefact — narrower signal, gets the
                          smallest weight because it depends on many
                          assumptions we have low confidence in)

Stateful (v2) specs re-weight to walk_forward 0.50 / monte_carlo 0.10
(design doc §5.3): the return-permutation Monte-Carlo test is biased
against defensive path-dependent strategies, so it counts for less and
the state-aware walk-forward counts for more. See _WEIGHT_*_STATEFUL.

Confidence band: ±10 points on the score. The score itself is a
weighted sum of four noisy signals, so a fixed ±10 band is honest
about the uncertainty without pretending we have a calibrated CI.
"""

from __future__ import annotations

from typing import Final

import structlog
from marketmind_shared.schemas import (
    DeflatedSharpeResult,
    MonteCarloResult,
    OverfittingScore,
    OverfittingVerdict,
    ParameterSweepResult,
    SignalContribution,
    StrategySpec,
    WalkForwardResult,
)
from marketmind_shared.schemas.strategy_spec import spec_uses_stateful_v2

log = structlog.get_logger(__name__)


# ---- Weights -------------------------------------------------------------

_WEIGHT_WALK_FORWARD: Final[float] = 0.35
_WEIGHT_PARAMETER_SWEEP: Final[float] = 0.25
_WEIGHT_MONTE_CARLO: Final[float] = 0.25
_WEIGHT_DEFLATED_SHARPE: Final[float] = 0.15

# Stateful (v2) specs re-weight (design doc §5.3): the return-permutation
# Monte-Carlo test compares against drift-preserving reshuffles and
# understates a defensive path-dependent strategy, so it is weighted down
# and the now state-aware walk-forward weighted up. Parameter-sweep (0.25)
# and deflated-Sharpe (0.15) are unchanged; the four still sum to 1.0.
_WEIGHT_WALK_FORWARD_STATEFUL: Final[float] = 0.50
_WEIGHT_MONTE_CARLO_STATEFUL: Final[float] = 0.10

# Verdict thresholds.
_THRESHOLD_ROBUST_MAX: Final[float] = 30.0
_THRESHOLD_OVERFIT_MIN: Final[float] = 60.0

# Confidence band (symmetric).
_CONFIDENCE_BAND_PTS: Final[float] = 10.0


# ---- Per-signal contribution mappers --------------------------------------


def _walk_forward_contribution(wf: WalkForwardResult) -> float:
    """
    degradation_ratio > 0.8: 0 pts
    0.5-0.8: linear 0 → 60 (well, 30 → 60 to be precise per spec)
    < 0.5:  linear 60 → 100 (mapped to ratio in [-0.5, 0.5])

    Per the spec:
      degradation > 0.8: contribute 0
      degradation 0.5-0.8: contribute 30-60 linearly
      degradation < 0.5: contribute 60-100

    Edge case: degradation_ratio_valid=False (IS_avg <= 0) → high
    overfitting suspicion because the strategy didn't even work
    in-sample; flag as 75 pts (between "mixed signals" and "overfit").
    """
    if not wf.degradation_ratio_valid:
        return 75.0
    r = wf.degradation_ratio
    if r > 0.8:
        return 0.0
    if r >= 0.5:
        # Linear from (0.5 → 60) to (0.8 → 30).
        return 60.0 - ((r - 0.5) / 0.3) * 30.0
    # r < 0.5. Linear from (0.5 → 60) to (-0.5 → 100). Clamp below.
    raw = 60.0 + ((0.5 - max(r, -0.5)) / 1.0) * 40.0
    return min(100.0, max(60.0, raw))


def _parameter_sweep_contribution(sweep: ParameterSweepResult) -> float:
    """
    peakiness < 0.3: contribute 0-20 (linear in [0, 0.3] → [0, 20])
    0.3-0.7: contribute 20-60 linearly
    > 0.7: contribute 60-100 linearly in [0.7, 1.0] → [60, 100]

    Edge case: no axes detected (`n_combinations == 0`) → we can't
    say anything about peakiness, return 30 (the upper edge of
    "robust") as a non-judgemental default.
    """
    if sweep.n_combinations == 0:
        return 30.0
    p = sweep.peakiness_score
    if p < 0.3:
        return (p / 0.3) * 20.0
    if p < 0.7:
        return 20.0 + ((p - 0.3) / 0.4) * 40.0
    return 60.0 + ((p - 0.7) / 0.3) * 40.0


def _monte_carlo_contribution(mc: MonteCarloResult) -> float:
    """
    p_value < 0.05: contribute 0-20 (linear in [0, 0.05] → [0, 20])
    0.05-0.20: contribute 20-50 linearly
    > 0.20: contribute 50-100 linearly in [0.20, 1.0] → [50, 100]
    """
    if mc.n_permutations == 0:
        return 50.0
    p = mc.p_value
    if p < 0.05:
        return (p / 0.05) * 20.0
    if p < 0.20:
        return 20.0 + ((p - 0.05) / 0.15) * 30.0
    return 50.0 + ((p - 0.20) / 0.80) * 50.0


def _deflated_sharpe_contribution(d: DeflatedSharpeResult) -> float:
    """
    probability > 0.95: contribute 0-20 (linear in [0.95, 1.0] → [20, 0])
    0.5-0.95: contribute 20-60 linearly
    < 0.5: contribute 60-100 linearly
    """
    p = d.probability_strategy_is_real
    if p >= 0.95:
        # Higher prob → lower pts. Map (0.95, 1.0) → (20, 0).
        return 20.0 * (1.0 - (p - 0.95) / 0.05)
    if p >= 0.5:
        # (0.5, 0.95) → (60, 20). Lower prob → higher pts.
        return 20.0 + ((0.95 - p) / 0.45) * 40.0
    # p < 0.5. (0.0, 0.5) → (100, 60). Lower prob → higher pts.
    return 60.0 + ((0.5 - p) / 0.5) * 40.0


# ---- Verdict -------------------------------------------------------------


def _classify(score: float) -> OverfittingVerdict:
    if score < _THRESHOLD_ROBUST_MAX:
        return OverfittingVerdict.LIKELY_ROBUST
    if score < _THRESHOLD_OVERFIT_MIN:
        return OverfittingVerdict.MIXED_SIGNALS
    return OverfittingVerdict.LIKELY_OVERFIT


# ---- Explanation generator -----------------------------------------------


def _build_explanation(
    score: float,
    verdict: OverfittingVerdict,
    contributions: list[SignalContribution],
    *,
    is_stateful: bool,
) -> str:
    """A 2-4 sentence summary. Cites the top 2 contributing signals
    and frames the verdict in plain English. For a stateful spec a
    closing note explains the Monte-Carlo down-weight (design doc §5.3).
    """
    sorted_contribs = sorted(contributions, key=lambda c: -c.contribution_pts)
    top_two = sorted_contribs[:2]

    if verdict is OverfittingVerdict.LIKELY_ROBUST:
        opener = (
            f"This strategy looks robust (score {score:.0f}/100). "
            f"The four overfitting checks agree the edge is unlikely to be a fluke."
        )
    elif verdict is OverfittingVerdict.MIXED_SIGNALS:
        opener = (
            f"Mixed signals (score {score:.0f}/100). Some checks support "
            f"the strategy and others raise concerns — treat the headline "
            f"return with caution."
        )
    else:
        opener = (
            f"This strategy looks overfit (score {score:.0f}/100). The checks "
            f"below suggest the backtest return is unlikely to repeat on new data."
        )

    bullets: list[str] = []
    for c in top_two:
        bullets.append(_describe_contribution(c))
    text = opener + " " + " ".join(bullets)

    if is_stateful:
        text += (
            " Note: this is a path-dependent (stateful) strategy. The Monte "
            "Carlo permutation test compares against drift-preserving "
            "reshuffles and can understate a defensive stateful strategy, so "
            "it is weighted lower (0.10 vs 0.25) and walk-forward higher "
            "(0.50 vs 0.35) in this score."
        )
    return text


def _describe_contribution(c: SignalContribution) -> str:
    if c.name == "walk_forward":
        return (
            f"Walk-forward: out-of-sample returns degraded to {c.raw_value:.2f}× "
            f"in-sample (degradation ratio)."
        )
    if c.name == "parameter_sweep":
        return (
            f"Parameter sweep: peakiness {c.raw_value:.2f} "
            f"({'sharp peak — suggests curve-fitting' if c.raw_value > 0.5 else 'flat plateau — robust'})."
        )
    if c.name == "monte_carlo":
        return (
            f"Monte Carlo: p-value {c.raw_value:.2f} "
            f"({'edge survives time-shuffling' if c.raw_value < 0.1 else 'edge mostly disappears on shuffled data'})."
        )
    return f"Deflated Sharpe: probability the edge is real = {c.raw_value:.2f}."


# ---- Public entry --------------------------------------------------------


def compute_overfitting_score(
    spec: StrategySpec,
    walk_forward: WalkForwardResult,
    sweep: ParameterSweepResult,
    monte_carlo: MonteCarloResult,
    deflated: DeflatedSharpeResult,
) -> OverfittingScore:
    """Fold the four signals into a single 0-100 score + verdict.

    For a stateful (v2) `spec` the Monte-Carlo signal is down-weighted and
    walk-forward up-weighted (design doc §5.3): the return-permutation MC
    test compares against drift-preserving reshuffles and understates a
    defensive path-dependent strategy. v1 specs keep the original weights.
    """
    is_stateful = spec_uses_stateful_v2(spec)
    w_wf = _WEIGHT_WALK_FORWARD_STATEFUL if is_stateful else _WEIGHT_WALK_FORWARD
    w_mc = _WEIGHT_MONTE_CARLO_STATEFUL if is_stateful else _WEIGHT_MONTE_CARLO
    w_sweep = _WEIGHT_PARAMETER_SWEEP
    w_ds = _WEIGHT_DEFLATED_SHARPE

    wf_pts = _walk_forward_contribution(walk_forward)
    sw_pts = _parameter_sweep_contribution(sweep)
    mc_pts = _monte_carlo_contribution(monte_carlo)
    ds_pts = _deflated_sharpe_contribution(deflated)

    contributions: list[SignalContribution] = [
        SignalContribution(
            name="walk_forward",
            label="Walk-forward degradation",
            raw_value=walk_forward.degradation_ratio
            if walk_forward.degradation_ratio_valid
            else 0.0,
            weight=w_wf,
            contribution_pts=wf_pts,
        ),
        SignalContribution(
            name="parameter_sweep",
            label="Parameter peakiness",
            raw_value=sweep.peakiness_score,
            weight=w_sweep,
            contribution_pts=sw_pts,
        ),
        SignalContribution(
            name="monte_carlo",
            label="Monte Carlo p-value",
            raw_value=monte_carlo.p_value,
            weight=w_mc,
            contribution_pts=mc_pts,
        ),
        SignalContribution(
            name="deflated_sharpe",
            label="Deflated Sharpe probability",
            raw_value=deflated.probability_strategy_is_real,
            weight=w_ds,
            contribution_pts=ds_pts,
        ),
    ]

    raw_score = wf_pts * w_wf + sw_pts * w_sweep + mc_pts * w_mc + ds_pts * w_ds
    score = max(0.0, min(100.0, raw_score))
    verdict = _classify(score)
    explanation = _build_explanation(score, verdict, contributions, is_stateful=is_stateful)

    return OverfittingScore(
        score=score,
        verdict=verdict,
        contributions=contributions,
        explanation=explanation,
        confidence_band_low=max(0.0, score - _CONFIDENCE_BAND_PTS),
        confidence_band_high=min(100.0, score + _CONFIDENCE_BAND_PTS),
    )


__all__ = ["compute_overfitting_score"]
