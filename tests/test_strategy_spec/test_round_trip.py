"""Canonical-JSON round trip: dump→reload produces the same canonical form.

We don't compare byte-for-byte against the on-disk fixture (which would
fail because we add defaults like position_sizing). Instead the invariant
is: the dumped representation is a fixed point — dumping it again yields
identical bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

from marketmind_shared.schemas.strategy_spec import validate_spec

from .conftest import load_json


def _canonical_json(obj: object) -> str:
    """Stable JSON: keys sorted, no extra whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def test_round_trip_is_fixed_point(valid_fixture_path: Path) -> None:
    data = load_json(valid_fixture_path)
    spec, _ = validate_spec(data)

    once = spec.model_dump(mode="json")
    canonical_once = _canonical_json(once)

    spec_again, _ = validate_spec(once)
    twice = spec_again.model_dump(mode="json")
    canonical_twice = _canonical_json(twice)

    assert canonical_once == canonical_twice
    assert spec == spec_again
