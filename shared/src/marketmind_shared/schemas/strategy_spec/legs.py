"""Phase E.3 — multi-leg / market-neutral spread schema (ADDITIVE).

A pre-E.3 spec is SINGLE-LEG: it has a primary ``instrument`` + top-level
``direction`` and nothing here. Phase E.3 adds two OPTIONAL fields to
``StrategySpec`` — ``legs`` and ``spread`` — that default to ``None``, so
every existing spec (the 7 live spot-long-only strategies, the whole
1538-test corpus) parses and runs BYTE-IDENTICALLY. The new shape activates
only when a spec opts in by setting both fields.

DESIGN (kept deliberately self-contained to protect the live trader):
the multi-leg spread strategy is NOT threaded through the generic
single-leg Condition/Exit machinery (that would touch every dispatcher and
risk the additive guarantee). Instead a multi-leg spec carries its own
``legs`` + ``spread`` config and is simulated by a dedicated perp-pair
engine (``workers/.../backtest/perp_pairs.py``). The single-leg engine
(vbt + iterative) is untouched.

CONVENTION — "leg A" is the spec's primary ``instrument``; "leg B" is
``legs[0].instrument``. The spread is built A-vs-B. A canonical
"long-spread" position is long leg A + short leg B, each at its leg
``weight`` (the hedge ratio). The mean-reversion simulator also takes the
INVERSE position (short A / long B) on the opposite z-extreme.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.strategy_spec.common import (
    Direction,
    Instrument,
    _StrictModel,
)


class SpreadLeg(_StrictModel):
    """An ADDITIONAL leg beyond the spec's primary ``instrument`` (leg A).

    ``direction`` is the leg's side in the canonical "long-spread" position
    (for a dollar-neutral pair, leg A is long and leg B is short — opposite
    sides). ``weight`` is the leg's notional weight relative to leg A: 1.0 is
    dollar-neutral; a beta-hedge ratio (e.g. 0.8) under-weights the more
    volatile leg. Each leg names its own instrument, so it carries its own
    ``symbol`` + ``asset_class`` (e.g. ``crypto_perp``).
    """

    instrument: Instrument
    direction: Direction
    weight: float = Field(
        default=1.0,
        gt=0.0,
        le=100.0,
        description="Notional weight of this leg relative to leg A (the hedge ratio).",
    )


class SpreadConfig(_StrictModel):
    """The spread definition + mean-reversion signal for a multi-leg spec.

    The spread series is built over leg A (``spec.instrument``) and leg B
    (``spec.legs[0].instrument``):

        log   : log(A.close) - log(B.close)   [the E.4 choice]
        ratio : A.close / B.close

    The z-score of the spread over ``zscore_period`` is the signal: ENTER a
    spread position when ``|z| >= entry_z`` (stretched), FLATTEN when
    ``|z| <= exit_z`` (reverted). z below ``-entry_z`` => long the spread
    (long A / short B, expecting it to rise); z above ``+entry_z`` => short
    the spread (short A / long B). The optional rolling-correlation gate
    (both ``corr_period`` and ``corr_min`` set) blocks NEW entries when the
    legs' rolling correlation drops below ``corr_min`` — the regime-decoupling
    tail-risk guard.
    """

    method: Literal["log", "ratio"] = Field(
        default="log",
        description="Spread construction. 'log' = log(A)-log(B); 'ratio' = A/B.",
    )
    zscore_period: int = Field(
        ge=2,
        le=10_000,
        description="Rolling window for the spread's mean + sample std (the z-score lookback).",
    )
    entry_z: float = Field(
        gt=0.0,
        le=20.0,
        description="Enter when the spread z-score magnitude reaches this (stretched).",
    )
    exit_z: float = Field(
        ge=0.0,
        le=20.0,
        description="Flatten when |z| falls to this (reverted). Must be < entry_z.",
    )
    stop_z: float | None = Field(
        default=None,
        gt=0.0,
        le=50.0,
        description=(
            "Optional divergence stop: flatten at a LOSS when |z| keeps widening "
            "to this (the spread diverged past the entry stretch instead of "
            "reverting — the primary pair loss mode). Must be > entry_z."
        ),
    )
    corr_period: int | None = Field(
        default=None,
        ge=2,
        le=10_000,
        description="Optional rolling-correlation window (regime gate). Set with corr_min.",
    )
    corr_min: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Optional min rolling correlation to allow new entries. Set with corr_period.",
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.exit_z >= self.entry_z:
            raise PydanticCustomError(
                "spread_exit_z_not_tighter",
                "spread.exit_z ({exit_z}) must be strictly less than entry_z "
                "({entry_z}) — the exit band is tighter than the entry band",
                {"exit_z": self.exit_z, "entry_z": self.entry_z},
            )
        if self.stop_z is not None and self.stop_z <= self.entry_z:
            raise PydanticCustomError(
                "spread_stop_z_not_wider",
                "spread.stop_z ({stop_z}) must be strictly greater than entry_z "
                "({entry_z}) — the divergence stop sits beyond the entry stretch",
                {"stop_z": self.stop_z, "entry_z": self.entry_z},
            )
        have_period = self.corr_period is not None
        have_min = self.corr_min is not None
        if have_period != have_min:
            raise PydanticCustomError(
                "spread_corr_params_partial",
                "spread correlation gate requires BOTH corr_period and "
                "corr_min, or NEITHER (got corr_period set: {p}, corr_min set: {m})",
                {"p": have_period, "m": have_min},
            )
        return self


__all__ = ["SpreadConfig", "SpreadLeg"]
