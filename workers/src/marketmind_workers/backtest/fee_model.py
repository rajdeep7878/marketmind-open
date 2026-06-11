"""Fee model — the abstraction backtest engines consume for per-fill commission.

Phase B.1 (2026-05-23). Replaces the path where the backtest engine read
``spec.costs.commission_pct`` directly. The spec field stays in the schema
for backward compatibility, but the backtest engine now derives commission
from this `FeeModel` — a static per-exchange / per-symbol / per-side /
per-volume-tier table.

This is the v2.0 "cost-model honesty at higher frequencies" foundation
the Phase B design doc Q1 + Q6 resolved:

  - Q1: static per-tier table (not live API). Quarterly manual refresh;
        documented in docs/operations/fees.md.
  - Q6: fees per (exchange, symbol, side); side defaults to "taker"
        (pessimist's assumption — backtests don't over-claim).

The default table reproduces v1's flat 10 bps for Binance Spot BTC/USDT
exactly. Existing seeded strategies' backtests are bit-identical because
their specs all carry ``commission_pct=0.001 == 10 bps`` and the FeeModel
default returns the same value for their (exchange, symbol). Future
strategies pick up the table's value automatically; the operator can
swap in a custom table by passing one to ``StaticFeeModel(table=…)`` or
loading from JSON via ``load_fee_model_from_json(path)``.

Slippage stays on ``spec.costs.slippage_pct`` for now — Phase B.2 ships
the sibling ``SlippageModel`` abstraction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from marketmind_shared.schemas.strategy_spec import AssetClass
from pydantic import BaseModel, Field

Side = Literal["maker", "taker"]


class FeeTier(BaseModel):
    """One volume tier: fees in bps for notional at or above the threshold."""

    volume_30d_usd_min: float = Field(ge=0.0)
    bps: float = Field(ge=0.0, le=1000.0)  # 0%..10% — anything more is absurd


# Outermost key: exchange (e.g. "binance_spot"). Inner: symbol (e.g.
# "BTC/USDT"). Inner: Side ("maker" / "taker"). Inner: list of tiers
# (ascending volume thresholds; the highest tier the notional qualifies
# for wins). Keeping it as a plain dict so the static table can be
# round-tripped to/from JSON without ceremony.
FeeTable = dict[str, dict[str, dict[Side, list[FeeTier]]]]


# Conservative pessimist fallback when (exchange, symbol, side) isn't in
# the table. 10 bps matches v1's hardcoded default — preserves bit-
# identity for any strategy whose instrument we haven't tabulated.
_FALLBACK_BPS: float = 10.0

# Phase C C.2 (2026-05-26): per-asset-class commission fallbacks. The
# crypto_spot entry MUST equal the pre-C.2 _FALLBACK_BPS so existing
# strategies are bit-identical. FX / metals have zero commission (the
# cost is in the spread, surfaced via SlippageModel). Equities (Alpaca
# in C.9) charge zero commission on standard ETFs/single names on a
# practice account — but C.9 wires that with its own table; until then
# the dispatch raises a clear NotImplementedError naming the gap.
_FALLBACK_BPS_BY_ASSET_CLASS: dict[str, float] = {
    "crypto_spot": _FALLBACK_BPS,  # 10 bps — bit-identical with pre-C.2
    "fx_spot": 0.0,                # Oanda demo: no explicit commission
    "metals_spot": 0.0,            # Oanda XAU/USD: no explicit commission
    # Phase E.3 (2026-06-06): USDT-margined crypto perpetuals (Binance USDM).
    # CONSERVATIVE — set equal to crypto_spot (10 bps) pending a USDM-specific
    # fee-tier table. Real Binance USDM taker is ~4-5 bps (CHEAPER than spot),
    # so this OVERSTATES perp commission — the safe direction (never fabricate
    # edge). A market-neutral PAIR crosses this on BOTH legs, so the pair's
    # round-trip commission is 2x a single leg's.
    "crypto_perp": _FALLBACK_BPS,  # 10 bps (conservative; real USDM taker ~4-5)
    # equity_etf / equity_single: raise — wired in C.9 alongside AlpacaAdapter
}


# v1 fee constants, frozen as the default table. Binance Spot, BTC/USDT,
# single VIP 0 tier, maker = taker = 10 bps (i.e. 0.10%). Refresh
# quarterly — see docs/operations/fees.md for the procedure.
_DEFAULT_FEE_TABLE: FeeTable = {
    "binance_spot": {
        "BTC/USDT": {
            "taker": [FeeTier(volume_30d_usd_min=0.0, bps=10.0)],
            "maker": [FeeTier(volume_30d_usd_min=0.0, bps=10.0)],
        },
    },
}


class FeeModel(Protocol):
    """The interface backtest engines depend on."""

    def commission_for(
        self,
        exchange: str,
        symbol: str,
        side: Side,
        notional_30d_usd: float = 0.0,
        *,
        asset_class: AssetClass | None = None,
    ) -> float:
        """Return the per-fill commission as a fraction (e.g. 0.001 = 10 bps).

        Phase C C.2 (2026-05-26): `asset_class` is keyword-only, default
        None (v1.2.B signature-widening pattern). When None or
        `crypto_spot`, lookup is bit-identical with the pre-C.2 path;
        the 10 bps fallback applies. For `fx_spot` / `metals_spot` the
        fallback drops to 0 bps (commission-free venues; cost is in the
        spread, surfaced via SlippageModel). `equity_etf` / `equity_single`
        raise NotImplementedError naming the C.9 sub-phase where AlpacaAdapter
        wires the equity cost tables.
        """
        ...


class StaticFeeModel:
    """Lookup against a static `FeeTable`. The default model uses
    `_DEFAULT_FEE_TABLE` — Binance Spot BTC/USDT at 10 bps for both sides.

    Phase C C.2: an `asset_class`-aware fallback table replaces the
    flat `_FALLBACK_BPS` when the (exchange, symbol, side) lookup
    misses. Crypto strategies fall back to 10 bps verbatim; FX / metals
    fall back to 0 bps; equities raise NotImplementedError (gap closes
    at C.9). The internal `_lookup_table` helper preserves the
    exchange→symbol→side→tiers walk so the in-table path is unchanged.
    """

    def __init__(self, table: FeeTable | None = None) -> None:
        self._table: FeeTable = table if table is not None else _DEFAULT_FEE_TABLE

    def commission_for(
        self,
        exchange: str,
        symbol: str,
        side: Side,
        notional_30d_usd: float = 0.0,
        *,
        asset_class: AssetClass | None = None,
    ) -> float:
        table_match = self._lookup_table(exchange, symbol, side, notional_30d_usd)
        if table_match is not None:
            return table_match
        # Fallback path: dispatch on asset_class. None defaults to the
        # pre-C.2 fallback (10 bps) for backward compatibility with
        # callers that don't pass asset_class.
        return _fallback_commission_for_class(asset_class)

    def _lookup_table(
        self,
        exchange: str,
        symbol: str,
        side: Side,
        notional_30d_usd: float,
    ) -> float | None:
        """Walk the (exchange, symbol, side) tier table. Returns the matched
        commission fraction, or None if the lookup misses at any level —
        caller then applies the per-asset-class fallback.
        """
        exchange_block = self._table.get(exchange)
        if exchange_block is None:
            return None
        symbol_block = exchange_block.get(symbol)
        if symbol_block is None:
            return None
        tiers = symbol_block.get(side)
        if not tiers:
            return None
        # Ascending by min-volume; highest qualifying tier wins.
        ordered = sorted(tiers, key=lambda t: t.volume_30d_usd_min)
        chosen_bps = ordered[0].bps
        for tier in ordered:
            if notional_30d_usd >= tier.volume_30d_usd_min:
                chosen_bps = tier.bps
            else:
                break
        return chosen_bps / 10000.0


def _fallback_commission_for_class(asset_class: AssetClass | None) -> float:
    """Per-asset-class fallback commission (fraction, e.g. 0.001 = 10 bps).

    Phase C C.2 dispatch table. `None` and unknown values both route to
    the pre-C.2 default (10 bps) for backward compatibility — callers
    that don't pass asset_class get the legacy path verbatim.

    Equities raise NotImplementedError so a future fx-or-equity hunt
    can't silently use crypto rates while AlpacaAdapter ships in C.9.
    """
    if asset_class is None or asset_class == "crypto_spot":
        return _FALLBACK_BPS / 10000.0
    if asset_class in _FALLBACK_BPS_BY_ASSET_CLASS:
        return _FALLBACK_BPS_BY_ASSET_CLASS[asset_class] / 10000.0
    if asset_class in ("equity_etf", "equity_single"):
        raise NotImplementedError(
            f"FeeModel: asset_class={asset_class!r} fee defaults land at "
            "Phase C C.9 alongside the AlpacaAdapter implementation. "
            "Until then, equity strategies cannot be backtested with "
            "honest cost assumptions.",
        )
    # Defensive — every AssetClass Literal value is covered above. If a
    # future sub-phase widens AssetClass without updating this dispatch,
    # this raises rather than silently returning a wrong rate.
    raise NotImplementedError(
        f"FeeModel: no fallback commission for asset_class={asset_class!r}",
    )


def default_fee_model() -> StaticFeeModel:
    """A `StaticFeeModel` over `_DEFAULT_FEE_TABLE`. The backtest engines
    construct one of these per backtest unless an explicit model is
    passed in (tests, future operator override).
    """
    return StaticFeeModel()


def commission_for_spec(
    spec: object,  # StrategySpec — typed loose to avoid a circular import
    side: Side = "taker",
    model: FeeModel | None = None,
) -> float:
    """Resolve the per-fill commission for a `StrategySpec` via the
    `FeeModel`. The default side is `taker` per the design doc Q6
    (conservative pessimist: backtest assumes the worst-case fill type
    unless the strategy uses limit orders).

    Phase C C.2: reads `instrument.asset_class` (defaulted to crypto_spot
    in C.1.1) and threads it through to the underlying FeeModel so the
    per-class fallback fires when the (exchange, symbol, side) tier
    table doesn't cover the instrument. crypto_spot keeps the 10 bps
    fallback (bit-identical with pre-C.2); fx_spot / metals_spot get 0
    bps; equities raise.
    """
    instrument = spec.instrument  # type: ignore[attr-defined]
    exchange_key = _exchange_key(instrument.exchange)
    symbol = instrument.symbol
    # getattr defaults to None for duck-typed stubs that pre-date C.1.1;
    # None routes to the crypto_spot fallback in _fallback_commission_for_class,
    # preserving bit-identity for legacy callers that don't carry the field.
    asset_class: AssetClass | None = getattr(instrument, "asset_class", None)
    fee_model = model if model is not None else default_fee_model()
    return fee_model.commission_for(exchange_key, symbol, side, asset_class=asset_class)


def _exchange_key(exchange: str) -> str:
    """Map a `StrategySpec.instrument.exchange` value (e.g. "binance") to
    the fee table's exchange key (e.g. "binance_spot"). v1 only covers
    Binance Spot; other exchanges pass through and hit the fallback.
    """
    if exchange == "binance":
        return "binance_spot"
    return exchange


def load_fee_model_from_json(path: Path | str) -> StaticFeeModel:
    """Load a fee table from a JSON file. The file's shape mirrors
    `FeeTable` — one outer key per exchange, then symbol, then side, then
    a list of `{volume_30d_usd_min, bps}` tiers.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    table: FeeTable = {
        exchange: {
            symbol: {
                side: [FeeTier(**tier) for tier in tiers]  # type: ignore[arg-type]
                for side, tiers in sides.items()
            }
            for symbol, sides in symbols.items()
        }
        for exchange, symbols in raw.items()
    }
    return StaticFeeModel(table)


__all__ = [
    "FeeModel",
    "FeeTable",
    "FeeTier",
    "Side",
    "StaticFeeModel",
    "commission_for_spec",
    "default_fee_model",
    "load_fee_model_from_json",
]


# ---------------------------------------------------------------------------
# Phase C C.2 note: volume-unit scale differs across asset classes.
#
# Binance returns currency volume (e.g. BTC quantity for a BTC/USDT bar).
# Oanda returns tick count for FX/metals (e.g. number of price updates
# during the bar window) per C.1.6 live finding. Alpaca returns share
# count for equities. These are NOT comparable across asset classes
# at the per-bar level.
#
# Indicators that consume the `volume` column (VolumeSMA, OBV) are
# scale-invariant in their formulas — they care about RELATIVE volume
# (today vs N-day average) not absolute units — so this is a
# documentation concern, not a code fix. A strategy that compares
# crypto-volume to FX-volume directly would be a category error caught
# at extraction time (different specs for different assets).
# ---------------------------------------------------------------------------
