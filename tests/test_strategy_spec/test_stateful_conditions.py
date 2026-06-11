"""Tests for the v2.0 stateful schema elements — RatchetExpr,
RegimeStateCondition, PriorTradeCondition — plus the validator rules and
introspection helpers added for them in Phase A.

Style mirrors the rest of tests/test_strategy_spec/: dict payloads run
through `validate_spec`, error codes matched exactly.
"""

from __future__ import annotations

from typing import Any

import pytest
from marketmind_shared.schemas.strategy_spec import (
    PriorSignalCondition,
    PriorTradeCondition,
    RatchetExpr,
    RegimeStateCondition,
    StrategySpecValidationErrorGroup,
    validate_spec,
)
from marketmind_shared.schemas.strategy_spec.introspection import (
    condition_uses_prior_signal,
    condition_uses_stateful_v2,
    condition_uses_tier3,
    iter_conditions,
    iter_expressions,
    stateful_nesting_depth,
)
from pydantic import ValidationError

# ---- payload helpers ------------------------------------------------------


def _ema(period: int) -> dict[str, Any]:
    return {"kind": "indicator", "name": "ema", "params": {"period": period}}


def _crossover(fast: int, slow: int, direction: str = "above") -> dict[str, Any]:
    return {
        "type": "crossover",
        "series": _ema(fast),
        "threshold": _ema(slow),
        "direction": direction,
    }


def _regime(*, enter: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "regime_state",
        "enter_when": enter if enter is not None else _crossover(20, 50, "above"),
        "exit_when": _crossover(20, 50, "below"),
        "initial": False,
    }


def _ratchet(reset: str = "never", extremum: str = "max") -> dict[str, Any]:
    return {
        "kind": "ratchet",
        "source": {"kind": "price", "field": "close"},
        "extremum": extremum,
        "reset": reset,
    }


def _prior_signal(predicate: str = "last_would_have_won") -> dict[str, Any]:
    return {"type": "prior_signal", "predicate": predicate}


def _spec_dict(
    *,
    entry_condition: dict[str, Any] | None = None,
    schema_version: str = "1.0",
    exits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "name": "Stateful Test Strategy",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": entry_condition if entry_condition is not None else _crossover(20, 50),
            "order_type": "market",
        },
        "exit": {
            "exits": exits
            or [{"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}}],
        },
    }


def _codes(exc: StrategySpecValidationErrorGroup) -> set[str]:
    return {e.error_code for e in exc.errors}


# ---- RatchetExpr ----------------------------------------------------------


@pytest.mark.parametrize("extremum", ["max", "min"])
@pytest.mark.parametrize("reset", ["never", "per_trade"])
def test_ratchet_expr_valid_variants(extremum: str, reset: str) -> None:
    expr = RatchetExpr.model_validate(_ratchet(reset=reset, extremum=extremum))
    assert expr.extremum == extremum
    assert expr.reset == reset
    assert expr.kind == "ratchet"


def test_ratchet_reset_defaults_to_per_trade() -> None:
    expr = RatchetExpr.model_validate(
        {"kind": "ratchet", "source": {"kind": "price", "field": "close"}, "extremum": "max"},
    )
    assert expr.reset == "per_trade"


def test_ratchet_nested_direct_is_rejected() -> None:
    nested = {
        "kind": "ratchet",
        "source": _ratchet(),
        "extremum": "max",
        "reset": "never",
    }
    with pytest.raises(ValidationError) as exc:
        RatchetExpr.model_validate(nested)
    assert "ratchet_nested_unsupported" in str(exc.value)


def test_ratchet_nested_through_scaled_is_rejected() -> None:
    # ratchet -> scaled -> ratchet: transitive nesting must still be caught.
    nested = {
        "kind": "ratchet",
        "source": {"kind": "scaled", "expression": _ratchet(), "factor": 0.9},
        "extremum": "max",
        "reset": "never",
    }
    with pytest.raises(ValidationError) as exc:
        RatchetExpr.model_validate(nested)
    assert "ratchet_nested_unsupported" in str(exc.value)


# ---- RegimeStateCondition -------------------------------------------------


def test_regime_state_valid() -> None:
    cond = RegimeStateCondition.model_validate(_regime())
    assert cond.type == "regime_state"
    assert cond.initial is False


def test_regime_state_identical_triggers_rejected() -> None:
    same = _crossover(20, 50, "above")
    with pytest.raises(ValidationError) as exc:
        RegimeStateCondition.model_validate(
            {"type": "regime_state", "enter_when": same, "exit_when": same, "initial": False},
        )
    assert "regime_state_triggers_identical" in str(exc.value)


# ---- PriorTradeCondition --------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    ["last_won", "last_lost", "consecutive_losses_at_least", "consecutive_wins_at_least"],
)
def test_prior_trade_valid_predicates(predicate: str) -> None:
    cond = PriorTradeCondition.model_validate({"type": "prior_trade", "predicate": predicate, "n": 2})
    assert cond.predicate == predicate
    assert cond.n == 2


@pytest.mark.parametrize("bad_n", [0, 100_001, -1])
def test_prior_trade_n_out_of_bounds_rejected(bad_n: int) -> None:
    # v1.2.B (2026-05-24): upper bound widened from 100 to 100_000 to
    # accommodate `bars_since_last_at_least` use cases (a one-month
    # throttle at 15m is 2_880 bars). 100 is now a valid n; 100_001 is
    # the first rejected value above the ceiling.
    with pytest.raises(ValidationError):
        PriorTradeCondition.model_validate(
            {"type": "prior_trade", "predicate": "consecutive_losses_at_least", "n": bad_n},
        )


# ---- PriorSignalCondition -------------------------------------------------


@pytest.mark.parametrize(
    "predicate",
    ["last_would_have_won", "last_would_have_lost", "last_fired"],
)
def test_prior_signal_valid_predicates(predicate: str) -> None:
    cond = PriorSignalCondition.model_validate(_prior_signal(predicate))
    assert cond.type == "prior_signal"
    assert cond.predicate == predicate


def test_prior_signal_unknown_predicate_rejected() -> None:
    # prior_trade's predicates are NOT prior_signal's — last_won is a
    # prior_trade word and must be rejected here.
    with pytest.raises(ValidationError):
        PriorSignalCondition.model_validate(_prior_signal("last_won"))


def test_prior_signal_rejects_n_field() -> None:
    # prior_signal carries no run-length parameter — every predicate is a
    # last_* test. Passing `n` (a prior_trade-ism) is an extra field and
    # the strict model rejects it.
    with pytest.raises(ValidationError):
        PriorSignalCondition.model_validate(
            {"type": "prior_signal", "predicate": "last_fired", "n": 2},
        )


# ---- schema_version + cross-cutting validator -----------------------------


def test_v1_spec_still_validates_and_defaults_schema_version() -> None:
    spec, warnings = validate_spec(_spec_dict())
    assert spec.schema_version == "1.0"
    assert warnings == []
    assert condition_uses_stateful_v2(spec.entry.condition) is False


def test_schema_version_rejects_unknown_value() -> None:
    with pytest.raises(StrategySpecValidationErrorGroup):
        validate_spec(_spec_dict(schema_version="3.0"))


def test_regime_state_in_v1_spec_is_rejected() -> None:
    with pytest.raises(StrategySpecValidationErrorGroup) as exc:
        validate_spec(_spec_dict(entry_condition=_regime(), schema_version="1.0"))
    assert "stateful_requires_schema_v2" in _codes(exc.value)


def test_regime_state_in_v2_spec_validates() -> None:
    spec, _ = validate_spec(_spec_dict(entry_condition=_regime(), schema_version="2.0"))
    assert spec.schema_version == "2.0"
    assert condition_uses_stateful_v2(spec.entry.condition) is True


def test_ratchet_in_v1_spec_is_rejected() -> None:
    exit_cond = {
        "type": "condition",
        "condition": {
            "type": "compare",
            "left": {"kind": "price", "field": "close"},
            "op": "<",
            "right": _ratchet(reset="never"),
        },
    }
    with pytest.raises(StrategySpecValidationErrorGroup) as exc:
        validate_spec(_spec_dict(schema_version="1.0", exits=[exit_cond]))
    assert "stateful_requires_schema_v2" in _codes(exc.value)


def test_prior_signal_in_v1_spec_is_rejected() -> None:
    with pytest.raises(StrategySpecValidationErrorGroup) as exc:
        validate_spec(_spec_dict(entry_condition=_prior_signal(), schema_version="1.0"))
    assert "stateful_requires_schema_v2" in _codes(exc.value)


def test_prior_signal_in_v2_spec_validates_without_warnings() -> None:
    spec, warnings = validate_spec(
        _spec_dict(entry_condition=_prior_signal(), schema_version="2.0"),
    )
    assert spec.schema_version == "2.0"
    # prior_signal has no `n`, so — unlike prior_trade — it never emits the
    # unused-n soft warning. A prior_signal fixture must be warning-clean.
    assert warnings == []


def test_stateful_nesting_too_deep_is_rejected() -> None:
    cond = _regime()
    for _ in range(5):  # wrap to depth 6 — past the limit of 4
        cond = {
            "type": "regime_state",
            "enter_when": cond,
            "exit_when": _crossover(20, 50, "below"),
            "initial": False,
        }
    with pytest.raises(StrategySpecValidationErrorGroup) as exc:
        validate_spec(_spec_dict(entry_condition=cond, schema_version="2.0"))
    assert "stateful_nesting_too_deep" in _codes(exc.value)


# ---- prior_trade soft warning ---------------------------------------------


def test_prior_trade_unused_n_emits_soft_warning() -> None:
    cond = {"type": "prior_trade", "predicate": "last_won", "n": 3}
    spec, warnings = validate_spec(_spec_dict(entry_condition=cond, schema_version="2.0"))
    assert spec.schema_version == "2.0"
    assert any("ignores n" in w.message for w in warnings)
    assert all(w.severity == "warning" for w in warnings)


def test_prior_trade_n_one_emits_no_warning() -> None:
    cond = {"type": "prior_trade", "predicate": "last_won", "n": 1}
    _, warnings = validate_spec(_spec_dict(entry_condition=cond, schema_version="2.0"))
    assert not any("ignores n" in w.message for w in warnings)


# ---- introspection helpers ------------------------------------------------


def test_condition_uses_tier3_detects_prior_trade() -> None:
    spec, _ = validate_spec(
        _spec_dict(
            entry_condition={"type": "prior_trade", "predicate": "last_won", "n": 1},
            schema_version="2.0",
        ),
    )
    assert condition_uses_tier3(spec.entry.condition) is True


def test_condition_uses_tier3_detects_prior_signal() -> None:
    spec, _ = validate_spec(
        _spec_dict(entry_condition=_prior_signal(), schema_version="2.0"),
    )
    assert condition_uses_tier3(spec.entry.condition) is True


def test_condition_uses_prior_signal_distinguishes_from_prior_trade() -> None:
    # The semantic boundary: prior_trade and prior_signal are both Tier-3,
    # but condition_uses_prior_signal must tell them apart — only the
    # latter needs the simulator's signal-history + phantom machinery.
    sig, _ = validate_spec(_spec_dict(entry_condition=_prior_signal(), schema_version="2.0"))
    trd, _ = validate_spec(
        _spec_dict(
            entry_condition={"type": "prior_trade", "predicate": "last_won", "n": 1},
            schema_version="2.0",
        ),
    )
    assert condition_uses_prior_signal(sig.entry.condition) is True
    assert condition_uses_prior_signal(trd.entry.condition) is False
    # ...while both still route to the Tier-3 iterative engine.
    assert condition_uses_tier3(sig.entry.condition) is True
    assert condition_uses_tier3(trd.entry.condition) is True


def test_condition_uses_tier3_detects_per_trade_ratchet() -> None:
    exit_cond = {
        "type": "condition",
        "condition": {
            "type": "compare",
            "left": {"kind": "price", "field": "close"},
            "op": "<",
            "right": _ratchet(reset="per_trade"),
        },
    }
    spec, _ = validate_spec(_spec_dict(schema_version="2.0", exits=[exit_cond]))
    bt_exit = spec.exit.exits[0]
    assert condition_uses_tier3(bt_exit.condition) is True  # type: ignore[union-attr]


def test_condition_uses_tier3_false_for_reset_never_ratchet() -> None:
    spec, _ = validate_spec(
        _spec_dict(
            schema_version="2.0",
            exits=[
                {
                    "type": "condition",
                    "condition": {
                        "type": "compare",
                        "left": {"kind": "price", "field": "close"},
                        "op": "<",
                        "right": _ratchet(reset="never"),
                    },
                },
            ],
        ),
    )
    bt_exit = spec.exit.exits[0]
    assert condition_uses_stateful_v2(bt_exit.condition) is True  # type: ignore[union-attr]
    assert condition_uses_tier3(bt_exit.condition) is False  # type: ignore[union-attr]


def test_iter_conditions_walks_into_regime_triggers() -> None:
    spec, _ = validate_spec(_spec_dict(entry_condition=_regime(), schema_version="2.0"))
    types = [c.type for c in iter_conditions(spec.entry.condition)]
    # the regime itself plus the two crossover triggers
    assert types.count("regime_state") == 1
    assert types.count("crossover") == 2


def test_iter_expressions_walks_into_ratchet_source() -> None:
    expr = RatchetExpr.model_validate(
        {
            "kind": "ratchet",
            "source": {"kind": "scaled", "expression": {"kind": "price", "field": "high"}, "factor": 2.0},
            "extremum": "max",
            "reset": "never",
        },
    )
    kinds = [e.kind for e in iter_expressions(expr)]
    assert kinds == ["ratchet", "scaled", "price"]


def test_stateful_nesting_depth_counts_only_regimes() -> None:
    spec, _ = validate_spec(_spec_dict(entry_condition=_regime(), schema_version="2.0"))
    assert stateful_nesting_depth(spec.entry.condition) == 1
