"""FTR strategy specs — frozen pydantic models, model_validate-only.

These are new spec models (the repo's StrategySpec v2.0 has no vocabulary
for ML probability gates, portfolio sizing, or microstructure inputs) but
follow its conventions: frozen models, extra='forbid', construction through
``model_validate`` — NEVER positional (repo footgun, see INTEGRATION_PLAN).

UK retail compliance guard (non-negotiable constraint 8): crypto
derivatives/perps/futures/CFDs are prohibited for UK retail (FCA ban in
force since Jan 2021; the Oct 2025 change opened crypto ETNs only, not
derivatives). ``FTRInstrument`` rejects any non-spot instrument type for
any execution mode unless the owning spec is ``research_simulation_only``,
and even then the paper trader refuses such specs by type
(test_ftr_uk_compliance_guard).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from marketmind_workers.ftr.config.venue_profiles import VENUE_PROFILES

_PROHIBITED_INSTRUMENT_KINDS = ("perp", "perpetual", "future", "futures", "cfd", "swap", "margin")


class FTRInstrument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=3, max_length=32)
    exchange: str = Field(min_length=1, max_length=32)
    instrument_type: Literal["spot"] = "spot"  # the ONLY representable type

    @model_validator(mode="after")
    def _no_derivative_symbols(self) -> FTRInstrument:
        sym = self.symbol.lower()
        for kind in _PROHIBITED_INSTRUMENT_KINDS:
            if kind in sym:
                raise ValueError(
                    f"instrument {self.symbol!r} looks like a {kind} — crypto derivatives are "
                    "prohibited for UK retail (FCA ban, in force since Jan 2021). "
                    "FTR execution is spot-only, long/flat-only."
                )
        return self


class _FTRSpecBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str = Field(min_length=1, max_length=64)
    venue_profile: str
    use_liquidity_overlay: bool = False
    # research_simulation_only is declared per-subclass: plain bool=False on
    # tradeable specs, frozen Literal[True] on OFIResearchSpec (so the paper
    # trader can refuse it by type, not by flag value).

    @model_validator(mode="after")
    def _known_profile(self) -> _FTRSpecBase:
        if self.venue_profile not in VENUE_PROFILES:
            raise ValueError(f"unknown venue profile {self.venue_profile!r}")
        return self


class MLHourlySpec(_FTRSpecBase):
    """3.1 ml_hourly_btc_longflat."""

    kind: Literal["ml_hourly_longflat"] = "ml_hourly_longflat"
    research_simulation_only: bool = False
    instrument: FTRInstrument
    horizon_bars: int = Field(ge=1, le=48, default=12)
    model_family: Literal["xgboost", "logistic"] = "xgboost"
    p_min: float = Field(gt=0.5, lt=1.0, default=0.55)
    safety_margin_bps: float = Field(ge=0.0, default=10.0)
    exit_hysteresis: float = Field(ge=0.0, le=0.1, default=0.02)
    trail_atr_multiple: float = Field(gt=0.0, default=2.5)
    # max holding = max_hold_horizon_multiple * horizon_bars
    max_hold_horizon_multiple: int = Field(ge=1, le=4, default=2)
    seed: int = 1729

    @model_validator(mode="after")
    def _short_horizons_reference_only(self) -> MLHourlySpec:
        # H in {1, 2} is evaluated on binance_spot_reference only — at
        # UK-feasible cost levels a 1-2h horizon cannot clear the EV floor
        # and running it there would just burn compute to document a
        # foregone REJECT. (Infeasible-venue research, mandate §3.1.)
        if self.horizon_bars <= 2 and self.venue_profile != "binance_spot_reference":
            raise ValueError(
                f"H={self.horizon_bars} is research-only on binance_spot_reference "
                f"(got profile {self.venue_profile!r})"
            )
        return self


class TrendPortfolioSpec(_FTRSpecBase):
    """3.2 trend_4h_portfolio."""

    kind: Literal["trend_4h_portfolio"] = "trend_4h_portfolio"
    research_simulation_only: bool = False
    exchange: str = "binance"
    timeframe: Literal["4h", "6h"] = "4h"
    universe_size: int = Field(ge=2, le=12, default=8)
    min_listed_days: int = Field(ge=180, default=540)
    ema_fast: int = Field(ge=5, default=50)
    ema_slow: int = Field(ge=20, default=200)
    donchian_n: int = Field(ge=10, default=55)
    chandelier_atr_multiple: float = Field(gt=0.0, default=3.0)
    target_sleeve_vol_annual: float = Field(gt=0.0, le=1.0, default=0.20)
    per_asset_cap_pct: float = Field(gt=0.0, le=0.25, default=0.25)
    gross_cap_pct: float = Field(gt=0.0, le=1.0, default=1.00)
    reentry_cooldown_hours: int = Field(ge=0, default=24)
    btc_regime_gate: bool = False

    @model_validator(mode="after")
    def _fast_below_slow(self) -> TrendPortfolioSpec:
        if self.ema_fast >= self.ema_slow:
            raise ValueError(f"ema_fast ({self.ema_fast}) must be < ema_slow ({self.ema_slow})")
        return self


class OFIResearchSpec(_FTRSpecBase):
    """3.3 ofi_microstructure_research.

    ``research_simulation_only`` is frozen True here: this spec may be
    backtested but is refused by the paper trader BY TYPE. Expected verdict
    per the evidence priors (Cont-Kukanov-Stoikov 2014; Silantyev 2019):
    REJECTED in taker mode — OFI-predicted moves are sub-spread at retail
    fees. The module exists to measure spread/depth/adverse-selection for
    the liquidity overlay and to seed future maker-side research, not to
    claim profit.
    """

    kind: Literal["ofi_microstructure_research"] = "ofi_microstructure_research"
    instrument: FTRInstrument
    research_simulation_only: Literal[True] = True
    horizon: Literal["1m", "5m", "15m"] = "5m"
    conviction_decile: float = Field(ge=0.8, le=0.99, default=0.90)
    cooldown_minutes: int = Field(ge=1, default=30)
    daily_signal_cap: int = Field(ge=1, le=9, default=8)
    execution_mode_sim: Literal["taker", "pessimistic_maker"] = "taker"
    decision_latency_ms: int = Field(ge=0, default=500)
    seed: int = 1729


class LiquidityOverlaySpec(BaseModel):
    """3.4 liquidity_overlay — a filter config, not a strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spread_percentile_max: float = Field(gt=0.0, lt=1.0, default=0.60)
    liquidity_score_min: float = Field(ge=0.0, le=1.0, default=0.30)
    max_defer_bars: int = Field(ge=0, le=10, default=2)
    trailing_days: int = Field(ge=7, default=30)


FTRStrategySpec = MLHourlySpec | TrendPortfolioSpec | OFIResearchSpec


def validate_ftr_spec(data: dict[str, object]) -> FTRStrategySpec:
    """The FTR analogue of the repo's validate_spec: dict in, typed spec out."""
    kind = data.get("kind")
    by_kind: dict[object, type[MLHourlySpec] | type[TrendPortfolioSpec] | type[OFIResearchSpec]] = {
        "ml_hourly_longflat": MLHourlySpec,
        "trend_4h_portfolio": TrendPortfolioSpec,
        "ofi_microstructure_research": OFIResearchSpec,
    }
    cls = by_kind.get(kind)
    if cls is None:
        raise ValueError(f"unknown FTR spec kind {kind!r}; known: {sorted(map(str, by_kind))}")
    return cls.model_validate(data)
