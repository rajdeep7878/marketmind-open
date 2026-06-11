"""Strategy template registry + build factory.

The signal engine builds one template instance per
`(strategy_version, symbol)` at startup via
`build_template(version.template, version.parameters)`. The returned
instance carries already-validated params, so per-cycle
`evaluate(candles, position)` is fast — no Pydantic parsing in the
hot path.

Adding a new template requires four coordinated changes:
  1. New `TemplateName` member in
     `marketmind_shared.schemas.trader`.
  2. Corresponding CHECK-constraint value in migration 0006 (or a
     new migration) + update the `_ENUM_TO_CHECK` mapping in
     `workers/tests/test_trader_enum_db_parity.py`.
  3. Concrete `Params` model + template class in a new submodule
     of `workers/marketmind_workers/trader/templates/`.
  4. Dispatch case in `build_template` below.
"""

from __future__ import annotations

from typing import Any

from marketmind_shared.schemas.trader import TemplateName

from marketmind_workers.trader.templates.base import (
    StrategyTemplate,
    TemplateParams,
    atr_stop_for_long,
    hold,
)
from marketmind_workers.trader.templates.bb_mean_reversion import (
    BbMeanReversionParams,
    BbMeanReversionTemplate,
)
from marketmind_workers.trader.templates.breakout import (
    BreakoutParams,
    BreakoutTemplate,
)
from marketmind_workers.trader.templates.ma_trend import (
    MaTrendParams,
    MaTrendTemplate,
)
from marketmind_workers.trader.templates.rsi_mean_reversion import (
    RsiMeanReversionParams,
    RsiMeanReversionTemplate,
)
from marketmind_workers.trader.templates.spec_template import SpecParams, SpecTemplate
from marketmind_workers.trader.templates.vcb import VcbParams, VcbTemplate


def build_template(name: TemplateName, raw_params: dict[str, Any]) -> StrategyTemplate:
    """Construct a typed template from a `TraderStrategyVersion`'s
    raw parameters dict.

    Raises `pydantic.ValidationError` if `raw_params` fails the
    template-specific schema. The signal engine handles validation
    failures by disabling the offending version and emitting an
    alert — the trader never proceeds with a partially-valid spec.
    """
    if name is TemplateName.MA_TREND:
        return MaTrendTemplate(MaTrendParams.model_validate(raw_params))
    if name is TemplateName.BREAKOUT:
        return BreakoutTemplate(BreakoutParams.model_validate(raw_params))
    if name is TemplateName.RSI_MEAN_REVERSION:
        return RsiMeanReversionTemplate(
            RsiMeanReversionParams.model_validate(raw_params),
        )
    if name is TemplateName.BB_MEAN_REVERSION:
        return BbMeanReversionTemplate(
            BbMeanReversionParams.model_validate(raw_params),
        )
    if name is TemplateName.VCB:
        return VcbTemplate(VcbParams.model_validate(raw_params))
    if name is TemplateName.SPEC:
        return SpecTemplate(SpecParams.model_validate(raw_params))
    # Unreachable: TemplateName is a StrEnum with exactly six members
    # and the if-chain covers every member. Pyright treats this as
    # exhaustive; the explicit raise documents the intent for a reader
    # unfamiliar with the enum's closed set.
    raise ValueError(f"unknown template {name!r}")


__all__ = [
    "BbMeanReversionParams",
    "BbMeanReversionTemplate",
    "BreakoutParams",
    "BreakoutTemplate",
    "MaTrendParams",
    "MaTrendTemplate",
    "RsiMeanReversionParams",
    "RsiMeanReversionTemplate",
    "SpecParams",
    "SpecTemplate",
    "StrategyTemplate",
    "TemplateParams",
    "VcbParams",
    "VcbTemplate",
    "atr_stop_for_long",
    "build_template",
    "hold",
]
