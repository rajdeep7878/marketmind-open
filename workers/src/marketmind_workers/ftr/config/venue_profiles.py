"""Venue cost profiles — the core design object of the FTR module.

Repo-conventional equivalent of the mandated ``venue_profiles.yaml`` (this
repo's config convention is typed Python tables + pydantic-settings, no YAML;
see docs/INTEGRATION_PLAN.md §0). Field-for-field the same structure.

Every backtest, validation run, and paper-trader cost estimate must name its
profile. Verdicts are computed for every profile; deploy eligibility requires
a PASS on a profile with ``uk_execution_feasible=True``.

Fee schedules drift — the bps values below were taken from public schedules
in June 2026 and are parameterized config, not facts baked into strategy
logic. Verify current schedules before relying on a verdict commercially.

Reconciliation with the legacy per-side cost model (fee_model.py /
slippage_model.py): legacy BTC/USDT binance_spot = 10 bps fee + 5 bps
"slippage" per side, where the 5 bps bundles spread + impact => 30 bps
round-trip. FTR profiles split half-spread from slippage, so
``binance_spot_reference`` round-trip = 2*10 + 2*1 + 2*2 = 26 bps. An FTR
run uses ONLY its named profile's three terms — the legacy models are never
additionally applied (no double counting).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class HalfSpreadBps(BaseModel):
    """Per-asset half-spread in bps with a default for non-BTC symbols."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    btc: float = Field(gt=0)
    default: float = Field(gt=0)

    def for_symbol(self, symbol: str) -> float:
        base = symbol.split("/")[0].upper()
        return self.btc if base == "BTC" else self.default


class VenueProfile(BaseModel):
    """One venue's taker/maker cost structure, per side, in bps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    uk_execution_feasible: bool
    taker_fee_bps: float = Field(ge=0)
    maker_fee_bps: float = Field(ge=0)
    half_spread_bps: HalfSpreadBps
    slippage_bps: float = Field(ge=0)

    def round_trip_cost_bps(
        self,
        symbol: str = "BTC/USDT",
        *,
        multiplier: float = 1.0,
        maker: bool = False,
    ) -> float:
        """Taker/taker (default) round-trip cost in bps.

        ``2*fee + 2*half_spread + 2*slippage``, scaled by the cost-sensitivity
        multiplier. Maker mode swaps the fee only — a resting order still
        suffers slippage-equivalent adverse selection, which we keep rather
        than model queue position optimistically.
        """
        fee = self.maker_fee_bps if maker else self.taker_fee_bps
        per_side = fee + self.half_spread_bps.for_symbol(symbol) + self.slippage_bps
        return 2.0 * per_side * multiplier

    def per_side_cost_bps(self, symbol: str = "BTC/USDT", *, multiplier: float = 1.0) -> float:
        return self.round_trip_cost_bps(symbol, multiplier=multiplier) / 2.0


VENUE_PROFILES: dict[str, VenueProfile] = {
    "binance_spot_reference": VenueProfile(
        name="binance_spot_reference",
        # Geo-restricted for UK retail onboarding since the 2021-2023 FCA
        # actions; reference economics only, never a deploy target.
        uk_execution_feasible=False,
        taker_fee_bps=10.0,
        maker_fee_bps=10.0,
        half_spread_bps=HalfSpreadBps(btc=1.0, default=2.5),
        slippage_bps=2.0,
    ),
    "kraken_pro_uk_tier0": VenueProfile(
        name="kraken_pro_uk_tier0",
        uk_execution_feasible=True,
        taker_fee_bps=40.0,  # verify current schedule; base tier June 2026
        maker_fee_bps=25.0,
        half_spread_bps=HalfSpreadBps(btc=2.0, default=4.0),
        slippage_bps=3.0,
    ),
    "coinbase_advanced_uk_tier0": VenueProfile(
        name="coinbase_advanced_uk_tier0",
        uk_execution_feasible=True,
        taker_fee_bps=60.0,  # verify; some sources report 40/60 inverted
        maker_fee_bps=40.0,
        half_spread_bps=HalfSpreadBps(btc=2.0, default=4.0),
        slippage_bps=3.0,
    ),
}

COST_SENSITIVITY_MULTIPLIERS: tuple[float, ...] = (1.0, 1.5, 2.0)

DEFAULT_VERDICT_PROFILE = "kraken_pro_uk_tier0"


def get_profile(name: str) -> VenueProfile:
    try:
        return VENUE_PROFILES[name]
    except KeyError:
        known = ", ".join(sorted(VENUE_PROFILES))
        raise KeyError(f"unknown venue profile {name!r}; known: {known}") from None


def feasible_profiles() -> list[VenueProfile]:
    return [p for p in VENUE_PROFILES.values() if p.uk_execution_feasible]
