"""Round-trip cost math from venue profiles (+ sensitivity multipliers).

Single source of cost truth for FTR backtests, validation and the paper
trader. An FTR run uses ONLY its named profile's three per-side terms
(fee + half-spread + slippage); the legacy FeeModel/SlippageModel is never
additionally applied — reconciliation note in INTEGRATION_PLAN §3.
"""

from __future__ import annotations

from dataclasses import dataclass

from marketmind_workers.ftr.config.venue_profiles import (
    COST_SENSITIVITY_MULTIPLIERS,
    VenueProfile,
    get_profile,
)

# Legacy per-side model (fee_model.py + slippage_model.py): 10 bps fee +
# 5 bps slippage-incl-spread per side for BTC/USDT binance_spot. Reported
# alongside binance_spot_reference for cross-era comparability.
LEGACY_CRYPTO_ROUND_TRIP_BPS = 30.0


@dataclass(frozen=True)
class CostBreakdown:
    profile: str
    symbol: str
    multiplier: float
    per_side_bps: float
    round_trip_bps: float

    @property
    def round_trip_frac(self) -> float:
        return self.round_trip_bps * 1e-4


def cost_breakdown(
    profile: VenueProfile | str,
    symbol: str,
    *,
    multiplier: float = 1.0,
) -> CostBreakdown:
    prof = get_profile(profile) if isinstance(profile, str) else profile
    rt = prof.round_trip_cost_bps(symbol, multiplier=multiplier)
    return CostBreakdown(
        profile=prof.name,
        symbol=symbol,
        multiplier=multiplier,
        per_side_bps=rt / 2.0,
        round_trip_bps=rt,
    )


def sensitivity_breakdowns(profile: VenueProfile | str, symbol: str) -> list[CostBreakdown]:
    return [cost_breakdown(profile, symbol, multiplier=m) for m in COST_SENSITIVITY_MULTIPLIERS]
