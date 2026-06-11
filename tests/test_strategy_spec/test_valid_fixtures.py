"""Each valid fixture must:
- parse without raising
- round-trip cleanly (load → validate → dump → reload → equal)
- produce no soft warnings (the eight chosen fixtures are direction-clean)
"""

from __future__ import annotations

from pathlib import Path

from marketmind_shared.schemas.strategy_spec import StrategySpec, validate_spec

from .conftest import load_json


def test_valid_fixture_validates(valid_fixture_path: Path) -> None:
    data = load_json(valid_fixture_path)
    spec, warnings = validate_spec(data)
    assert isinstance(spec, StrategySpec)
    assert warnings == [], f"expected no warnings, got {warnings}"


def test_valid_fixture_round_trips(valid_fixture_path: Path) -> None:
    data = load_json(valid_fixture_path)
    spec, _ = validate_spec(data)

    # Pydantic-canonical dump (resolves defaults, normalizes ordering).
    dumped = spec.model_dump(mode="json")
    spec2, _ = validate_spec(dumped)
    assert spec == spec2

    # Re-dumping must be idempotent.
    dumped2 = spec2.model_dump(mode="json")
    assert dumped == dumped2
