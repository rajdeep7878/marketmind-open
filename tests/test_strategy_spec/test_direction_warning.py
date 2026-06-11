"""Direction-consistency is a soft warning, not a hard error.

Per spec validation rule #6: "A direction 'long' strategy's exits should
reference long-position semantics (stop below entry, etc.). Soft warning,
not hard rejection." The validator therefore must:
- still parse the spec successfully
- return an ExtractionNote describing the inconsistency

Two carrier patterns: percent stop with sign implying wrong direction, and
fixed-price stop+TP placed on the wrong side of each other.
"""

from __future__ import annotations

from marketmind_shared.schemas.strategy_spec import validate_spec


def _base_long_spec() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": "Direction warning carrier",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "1h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "compare",
                "left": {"kind": "price", "field": "close"},
                "op": ">",
                "right": {
                    "kind": "indicator",
                    "name": "sma",
                    "params": {"period": 20},
                },
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                # Percent stop with NEGATIVE value: implies stop ABOVE entry for long.
                {"type": "stop_loss", "method": {"kind": "percent", "value": -0.05}}
            ]
        },
    }


def test_long_with_negative_percent_stop_warns() -> None:
    spec, warnings = validate_spec(_base_long_spec())
    assert spec is not None
    assert warnings, "expected a direction-consistency warning"
    assert any(
        w.severity == "warning"
        and "direction consistency" in w.message
        and "ABOVE entry" in w.message
        for w in warnings
    ), [w.message for w in warnings]


def test_long_with_inverted_fixed_prices_warns() -> None:
    data = _base_long_spec()
    data["exit"] = {
        "exits": [
            # Stop above TP for a long — clearly a short-trade pattern.
            {"type": "stop_loss", "method": {"kind": "fixed_price", "price": 60000}},
            {"type": "take_profit", "method": {"kind": "fixed_price", "price": 50000}},
        ]
    }
    spec, warnings = validate_spec(data)
    assert spec is not None
    assert any(
        w.severity == "warning" and "direction consistency" in w.message for w in warnings
    ), [w.message for w in warnings]


def test_consistent_long_emits_no_warnings() -> None:
    # Sanity: the base spec with a POSITIVE percent stop should be clean.
    data = _base_long_spec()
    data["exit"] = {"exits": [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}]}
    _, warnings = validate_spec(data)
    assert warnings == []
