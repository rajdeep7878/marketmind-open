"""CostModel: per-trade transaction costs.

Defaults match Binance maker fees + a conservative slippage. Backtests
that pass through with zero costs will be flagged downstream — having
the field default to "reasonable" rather than zero prevents accidentally
running cost-free backtests.

NOTE (Phase B.1, B.2 — 2026-05-23): the backtest engines no longer
read ``commission_pct`` or ``slippage_pct`` from this model. Commission
is derived from ``workers/.../backtest/fee_model.py`` and slippage from
``workers/.../backtest/slippage_model.py`` — both static, per-exchange
/ per-symbol / per-side / per-tier tables. The values on this schema
are kept for **serialisation and UI display only** (round-tripping
extracted specs, showing the LLM's stated assumptions vs the engine's
actual cost model). The "defaulted" UI flag still surfaces when
``spec.costs == DEFAULT_COST_MODEL`` because the model defaults match
the engine's FeeModel / SlippageModel defaults exactly, so the
"defaulted" provenance signal is preserved.

The trader path is independent — ``trader_strategy_versions.fee_bps``
and ``.slippage_bps`` remain the trader's authoritative per-version
cost values; the FeeModel / SlippageModel do NOT feed the live trader.
"""

from __future__ import annotations

from pydantic import Field

from marketmind_shared.schemas.strategy_spec.common import _StrictModel


class CostModel(_StrictModel):
    # 0..1 each. Costs above 10% per side would be absurd; we cap at that.
    commission_pct: float = Field(default=0.001, ge=0.0, le=0.1)
    slippage_pct: float = Field(default=0.0005, ge=0.0, le=0.1)
    # Phase E.3 (2026-06-06): perpetual-swap funding. ADDITIVE + DECLARATIVE —
    # defaults to None so every pre-E.3 (spot) spec is byte-identical. Like
    # commission/slippage above, the perp engine does NOT read this for the
    # actual charge: real funding accrues from the 8h funding-rate FIXTURE on
    # MARK price, sign-correct per leg (see perp_pairs.py). This field only
    # records the spec's *stated* funding assumption for UI / round-tripping.
    # None on a non-perp spec; set (e.g. funding_model="binance_8h") on a perp.
    funding_model: str | None = Field(
        default=None,
        max_length=32,
        description=(
            "Declarative funding assumption for perp specs (e.g. "
            "'binance_8h'); None for spot. The engine charges real funding "
            "from the funding fixture on mark price, not from this field."
        ),
    )


DEFAULT_COST_MODEL: CostModel = CostModel()


__all__ = ["DEFAULT_COST_MODEL", "CostModel"]
