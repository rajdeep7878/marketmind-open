"""Slippage model — the abstraction backtest engines consume for per-fill slippage.

Phase B.2 (2026-05-23). Sibling to ``fee_model.py``. Replaces the path
where the backtest engine read ``spec.costs.slippage_pct`` directly.
The spec field stays in the schema for backward compatibility, but the
backtest engine now derives slippage from this ``SlippageModel`` — a
static per-exchange / per-symbol / per-side / per-volume-tier table,
mirroring ``FeeModel`` shape-for-shape.

This is the v2.0 "cost-model honesty at higher frequencies" foundation,
completed alongside ``FeeModel``. Together they let B.3+ (lower
timeframes) reason about cost-to-trade in a single place, since the
spread-implied slippage at 1H / 15m is no longer negligible.

Q2 resolution from the Phase B design (commit ``0223b8e``): spread-based
static assumption, NOT live L2-data dynamic. Quarterly manual refresh,
documented in ``docs/operations/slippage.md``. Revisit if/when Phase D.

The default table reproduces v1's ``CostModel.slippage_pct = 0.0005`` =
**5 bps** for Binance Spot BTC/USDT exactly. NOTE: this is HALF the
fee default (10 bps). The asymmetry is intentional — spreads on
BTC/USDT majors are tighter than the round-trip commission. Existing
seeded strategies' backtests are bit-identical because their specs all
carry the default 5 bps and the SlippageModel default returns the same
value for their (exchange, symbol).

The trader path is independent: ``trader_strategy_versions.slippage_bps``
remains the authoritative per-version override. The SlippageModel does
NOT feed the live trader; that's a future unification phase.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from marketmind_shared.schemas.strategy_spec import AssetClass
from pydantic import BaseModel, Field

Side = Literal["maker", "taker"]


class SlippageTier(BaseModel):
    """One volume tier: slippage in bps for notional at or above the threshold."""

    volume_30d_usd_min: float = Field(ge=0.0)
    bps: float = Field(ge=0.0, le=1000.0)  # 0%..10% — anything more is absurd


# Outermost key: exchange (e.g. "binance_spot"). Inner: symbol (e.g.
# "BTC/USDT"). Inner: Side ("maker" / "taker"). Inner: list of tiers
# (ascending volume thresholds; the highest tier the notional qualifies
# for wins). Shape mirrors FeeTable so the operator-facing config files
# (fees.json / slippage.json) are visually parallel.
SlippageTable = dict[str, dict[str, dict[Side, list[SlippageTier]]]]


# Conservative pessimist fallback when (exchange, symbol, side) isn't
# in the table. 5 bps matches v1's hardcoded default — preserves bit-
# identity for any strategy whose instrument we haven't tabulated.
_FALLBACK_BPS: float = 5.0

# Phase C C.2 (2026-05-26): per-asset-class slippage fallbacks.
#  - crypto_spot keeps 5 bps (bit-identical with pre-C.2)
#  - fx_spot uses 5 bps (~1 pip on EUR/USD per C.1.6 live findings;
#    appropriately conservative for off-hours when spreads widen)
#  - metals_spot uses 12 bps (XAU/USD spreads are wider in % terms
#    per design doc §C.2 table; XAU at ~$2400 with a 30c spread =
#    ~12 bps round-trip)
#  - equity_etf / equity_single: NotImplementedError, gap closes at C.9
_FALLBACK_BPS_BY_ASSET_CLASS: dict[str, float] = {
    "crypto_spot": _FALLBACK_BPS,  # 5 bps — bit-identical with pre-C.2
    "fx_spot": 5.0,                # ≈ 1 pip on EUR/USD; conservative for off-hours
    "metals_spot": 12.0,           # XAU/USD spreads ~30c on $2400 = ~12 bps
    # Phase E.3 (2026-06-06): Binance USDM perpetuals. CONSERVATIVE — equal to
    # crypto_spot (5 bps) pending a USDM-specific table. Deep BTC/ETH perp books
    # are actually TIGHTER (~1-3 bps), so this overstates slippage (the safe
    # direction). A market-neutral PAIR pays this on BOTH legs.
    "crypto_perp": _FALLBACK_BPS,  # 5 bps (conservative; real USDM ~1-3)
    # equity_etf / equity_single: raise — wired in C.9 alongside AlpacaAdapter
}


# v1 slippage constants, frozen as the default table. Binance Spot,
# BTC/USDT, single VIP 0 tier, maker = taker = 5 bps (i.e. 0.05%).
# Note this is HALF the fee default (10 bps) — spreads on BTC/USDT are
# tighter than round-trip commission, intentionally asymmetric. Refresh
# quarterly — see docs/operations/slippage.md for the procedure.
_DEFAULT_SLIPPAGE_TABLE: SlippageTable = {
    "binance_spot": {
        "BTC/USDT": {
            "taker": [SlippageTier(volume_30d_usd_min=0.0, bps=5.0)],
            "maker": [SlippageTier(volume_30d_usd_min=0.0, bps=5.0)],
        },
    },
}


class SlippageModel(Protocol):
    """The interface backtest engines depend on."""

    def slippage_for(
        self,
        exchange: str,
        symbol: str,
        side: Side,
        notional_30d_usd: float = 0.0,
        *,
        asset_class: AssetClass | None = None,
    ) -> float:
        """Return the per-fill slippage as a fraction (e.g. 0.0005 = 5 bps).

        Phase C C.2 (2026-05-26): keyword-only `asset_class` defaults to
        None (v1.2.B signature-widening pattern). None or `crypto_spot`
        gets the pre-C.2 5 bps fallback. `fx_spot` also 5 bps
        (~1 pip EUR/USD). `metals_spot` 12 bps (XAU wider). Equities
        raise NotImplementedError pointing to C.9.
        """
        ...


class StaticSlippageModel:
    """Lookup against a static ``SlippageTable``. The default model uses
    ``_DEFAULT_SLIPPAGE_TABLE`` — Binance Spot BTC/USDT at 5 bps for both sides.

    Phase C C.2: an `asset_class`-aware fallback table replaces the
    flat `_FALLBACK_BPS` when the (exchange, symbol, side) lookup
    misses. crypto + fx → 5 bps; metals → 12 bps; equities raise.
    The internal `_lookup_table` helper preserves the in-table path
    verbatim.
    """

    def __init__(self, table: SlippageTable | None = None) -> None:
        self._table: SlippageTable = table if table is not None else _DEFAULT_SLIPPAGE_TABLE

    def slippage_for(
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
        return _fallback_slippage_for_class(asset_class)

    def _lookup_table(
        self,
        exchange: str,
        symbol: str,
        side: Side,
        notional_30d_usd: float,
    ) -> float | None:
        """Walk the (exchange, symbol, side) tier table. Returns the matched
        slippage fraction, or None if the lookup misses at any level —
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


def _fallback_slippage_for_class(asset_class: AssetClass | None) -> float:
    """Per-asset-class fallback slippage (fraction, e.g. 0.0005 = 5 bps).

    Phase C C.2 dispatch table. `None` and `crypto_spot` both return
    the pre-C.2 fallback (5 bps) — backward compat for callers that
    don't pass asset_class.

    Equities raise NotImplementedError so an FX-or-equity hunt can't
    silently use crypto rates while AlpacaAdapter ships in C.9.
    """
    if asset_class is None or asset_class == "crypto_spot":
        return _FALLBACK_BPS / 10000.0
    if asset_class in _FALLBACK_BPS_BY_ASSET_CLASS:
        return _FALLBACK_BPS_BY_ASSET_CLASS[asset_class] / 10000.0
    if asset_class in ("equity_etf", "equity_single"):
        raise NotImplementedError(
            f"SlippageModel: asset_class={asset_class!r} slippage defaults "
            "land at Phase C C.9 alongside the AlpacaAdapter implementation. "
            "Until then, equity strategies cannot be backtested with honest "
            "spread assumptions.",
        )
    raise NotImplementedError(
        f"SlippageModel: no fallback slippage for asset_class={asset_class!r}",
    )


def default_slippage_model() -> StaticSlippageModel:
    """A ``StaticSlippageModel`` over ``_DEFAULT_SLIPPAGE_TABLE``. The
    backtest engines construct one of these per backtest unless an
    explicit model is passed in (tests, future operator override).
    """
    return StaticSlippageModel()


def slippage_for_spec(
    spec: object,  # StrategySpec — typed loose to avoid a circular import
    side: Side = "taker",
    model: SlippageModel | None = None,
) -> float:
    """Resolve the per-fill slippage for a ``StrategySpec`` via the
    ``SlippageModel``. The default side is ``taker`` per the same
    design-doc resolution that drives ``FeeModel`` (conservative
    pessimist: backtest assumes the worst-case fill type unless the
    strategy uses limit orders).

    Phase C C.2: mirrors ``commission_for_spec``'s asset_class threading.
    Reads ``instrument.asset_class`` (defaulted to crypto_spot in C.1.1)
    and passes it to the underlying model so the per-class fallback
    fires on table misses.
    """
    instrument = spec.instrument  # type: ignore[attr-defined]
    exchange_key = _exchange_key(instrument.exchange)
    symbol = instrument.symbol
    # getattr defaults to None for duck-typed stubs that pre-date C.1.1;
    # None routes to the crypto_spot fallback in _fallback_slippage_for_class,
    # preserving bit-identity for legacy callers that don't carry the field.
    asset_class: AssetClass | None = getattr(instrument, "asset_class", None)
    slippage_model = model if model is not None else default_slippage_model()
    return slippage_model.slippage_for(exchange_key, symbol, side, asset_class=asset_class)


def _exchange_key(exchange: str) -> str:
    """Map a ``StrategySpec.instrument.exchange`` value (e.g. "binance")
    to the slippage table's exchange key (e.g. "binance_spot"). Mirrors
    ``fee_model._exchange_key`` exactly — v1 only covers Binance Spot;
    other exchanges pass through and hit the fallback.
    """
    if exchange == "binance":
        return "binance_spot"
    return exchange


def load_slippage_model_from_json(path: Path | str) -> StaticSlippageModel:
    """Load a slippage table from a JSON file. The file's shape mirrors
    ``SlippageTable`` — one outer key per exchange, then symbol, then
    side, then a list of ``{volume_30d_usd_min, bps}`` tiers.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    table: SlippageTable = {
        exchange: {
            symbol: {
                side: [SlippageTier(**tier) for tier in tiers]  # type: ignore[arg-type]
                for side, tiers in sides.items()
            }
            for symbol, sides in symbols.items()
        }
        for exchange, symbols in raw.items()
    }
    return StaticSlippageModel(table)


__all__ = [
    "Side",
    "SlippageModel",
    "SlippageTable",
    "SlippageTier",
    "StaticSlippageModel",
    "default_slippage_model",
    "load_slippage_model_from_json",
    "slippage_for_spec",
]
