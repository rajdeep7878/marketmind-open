"""A.5b — round-trip + validation of the trader StrategyState models.

`StrategyState` is the `trader_strategy_state.state` JSONB payload; it
must survive a `model_dump` → JSON → `model_validate` cycle unchanged,
since that is exactly the write/read path through the JSONB column.
"""

from __future__ import annotations

import json

import pytest
from marketmind_shared.schemas.trader import (
    RatchetState,
    RegimeState,
    StrategyState,
    Tier3CompletedTrade,
    Tier3PendingPhantom,
    Tier3ShadowPosition,
    Tier3SignalRecord,
    Tier3State,
)
from pydantic import ValidationError


def _round_trip(state: StrategyState) -> StrategyState:
    """model_dump(json) → json.dumps → json.loads → model_validate — the
    exact path a StrategyState takes through a JSONB column.
    """
    as_json = json.loads(json.dumps(state.model_dump(mode="json")))
    return StrategyState.model_validate(as_json)


def test_empty_strategy_state_round_trips() -> None:
    state = StrategyState()
    assert state.regimes == []
    assert state.ratchets == []
    assert _round_trip(state) == state


def test_strategy_state_with_regimes_and_ratchets_round_trips() -> None:
    state = StrategyState(
        regimes=[RegimeState(latched=True), RegimeState(latched=False)],
        ratchets=[RatchetState(extremum=46060.74)],
    )
    restored = _round_trip(state)
    assert restored == state
    assert restored.regimes[0].latched is True
    assert restored.regimes[1].latched is False
    assert restored.ratchets[0].extremum == 46060.74


def test_ratchet_state_carries_reset_epoch_for_a6() -> None:
    # reset_epoch is unused in A.5 (defaults None) but must persist — A.6's
    # per-trade ratchets record the trade-entry epoch in it.
    state = StrategyState(ratchets=[RatchetState(extremum=100.0, reset_epoch=42)])
    restored = _round_trip(state)
    assert restored.ratchets[0].reset_epoch == 42


def test_ratchet_state_rejects_non_finite_extremum() -> None:
    # A persisted extremum is always finite; the in-memory cold-start seed
    # (-inf / +inf) must never reach the JSONB column.
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            RatchetState(extremum=bad)


def test_strategy_state_is_frozen() -> None:
    # State is an immutable value object — each advance builds a new one.
    state = StrategyState(regimes=[RegimeState(latched=True)])
    with pytest.raises(ValidationError):
        state.regimes = []  # type: ignore[misc]


# ---- A.6: Tier-3 checkpoint round-trip -------------------------------------


def test_strategy_state_with_tier3_round_trips() -> None:
    """The full Tier-3 checkpoint — resolved + pending signal records, a
    shadow position, a pending phantom — survives the JSONB round-trip.
    """
    state = StrategyState(
        tier3=Tier3State(
            signal_history=[
                Tier3SignalRecord(
                    signal_bar=10,
                    fired=True,
                    return_pct=0.05,
                    outcome="win",
                    resolved_bar=18,
                ),
                Tier3SignalRecord(signal_bar=25, fired=False),  # pending phantom
            ],
            trade_history=[
                Tier3CompletedTrade(
                    entry_index=10,
                    exit_index=18,
                    return_pct=0.05,
                    outcome="win",
                ),
            ],
            shadow_position=Tier3ShadowPosition(
                entry_bar=25,
                entry_fill=46000.0,
                size=0.5,
                stop_level=44000.0,
                trail_anchor=46500.0,
            ),
            pending_phantoms=[
                Tier3PendingPhantom(
                    signal_bar=25,
                    position=Tier3ShadowPosition(
                        entry_bar=25,
                        entry_fill=46000.0,
                        size=1.0,
                        trail_anchor=46000.0,
                    ),
                ),
            ],
            trade_id=1,
        ),
    )
    restored = _round_trip(state)
    assert restored == state
    assert restored.tier3 is not None
    assert restored.tier3.signal_history[0].outcome == "win"
    assert restored.tier3.signal_history[1].outcome is None  # still pending
    assert restored.tier3.pending_phantoms[0].signal_bar == 25


def test_tier1_tier2_strategy_state_has_no_tier3() -> None:
    # A Tier-1/Tier-2 spec's state carries no tier3 block — it stays None.
    state = StrategyState(regimes=[RegimeState(latched=True)])
    assert state.tier3 is None
    assert _round_trip(state) == state


def test_tier3_signal_record_rejects_non_finite_return() -> None:
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Tier3SignalRecord(signal_bar=1, fired=False, return_pct=bad)
