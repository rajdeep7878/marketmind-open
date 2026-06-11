"""Tests for the strategy-template registry / build_template dispatch."""

from __future__ import annotations

from typing import Any

import pytest
from marketmind_shared.schemas.trader import TemplateName
from marketmind_workers.trader.templates import (
    BbMeanReversionTemplate,
    BreakoutTemplate,
    MaTrendTemplate,
    RsiMeanReversionTemplate,
    SpecTemplate,
    VcbTemplate,
    build_template,
)
from pydantic import ValidationError

# The five v1 templates take all-defaulted params, so `{}` exercises
# their defaults. SpecTemplate carries a StrategySpec — it has no
# meaningful empty default, so the registry tests give it a minimal
# real spec (long, single-timeframe, no Tier-3, with a stop).
_MINIMAL_SPEC: dict[str, Any] = {
    "schema_version": "1.0",
    "name": "Registry-test spec",
    "instrument": {"symbol": "BTC/USDT", "exchange": "binance", "quote_currency": "USDT"},
    "primary_timeframe": "4h",
    "direction": "long",
    "entry": {
        "condition": {
            "type": "crossover",
            "series": {"kind": "price", "field": "close"},
            "threshold": {"kind": "constant", "value": 100.0},
            "direction": "above",
        },
        "order_type": "market",
    },
    "exit": {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]},
    "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
}
_SPEC_PARAMS: dict[str, Any] = {"spec": _MINIMAL_SPEC}


def _params_for(name: TemplateName) -> dict[str, Any]:
    """Build-params per template: SPEC needs a real spec, the rest default."""
    return _SPEC_PARAMS if name is TemplateName.SPEC else {}


@pytest.mark.parametrize(
    "name,expected_cls",
    [
        (TemplateName.MA_TREND, MaTrendTemplate),
        (TemplateName.BREAKOUT, BreakoutTemplate),
        (TemplateName.RSI_MEAN_REVERSION, RsiMeanReversionTemplate),
        (TemplateName.BB_MEAN_REVERSION, BbMeanReversionTemplate),
        (TemplateName.VCB, VcbTemplate),
        (TemplateName.SPEC, SpecTemplate),
    ],
)
def test_build_template_dispatches_each_known_name(
    name: TemplateName,
    expected_cls: type,
) -> None:
    """Every TemplateName member dispatches to its concrete template class."""
    template = build_template(name, _params_for(name))
    assert isinstance(template, expected_cls)
    assert template.template_name is name


def test_build_template_validates_params_at_construction() -> None:
    """A nonsense parameter for a known template raises
    ValidationError — invalid specs are rejected at startup rather
    than producing malformed signals at runtime.
    """
    with pytest.raises(ValidationError):
        build_template(
            TemplateName.MA_TREND,
            {"fast_ema_period": 100, "slow_ema_period": 10},  # fast >= slow
        )


def test_build_template_rejects_unknown_extra_params() -> None:
    """_StrictModel.extra='forbid' — typo'd params blow up loudly."""
    with pytest.raises(ValidationError):
        build_template(
            TemplateName.MA_TREND,
            {"banana_period": 50},
        )


def test_min_bars_needed_is_positive_for_every_template() -> None:
    """Every concrete template returns a sane positive bar count."""
    for name in TemplateName:
        template = build_template(name, _params_for(name))
        assert template.min_bars_needed() > 0
