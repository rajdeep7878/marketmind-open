"""Fixtures and helpers shared across strategy-spec tests.

Each test file uses these to walk the on-disk fixture directory rather
than re-encoding fixture data in Python — the JSON is the source of truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "strategies"
VALID_DIR = FIXTURES_ROOT / "valid"
INVALID_DIR = FIXTURES_ROOT / "invalid"


def _valid_fixture_paths() -> list[Path]:
    return sorted(VALID_DIR.glob("*.json"))


def _invalid_fixture_paths() -> list[Path]:
    # The .json files; the sibling .expected_error.json files are looked up
    # by name in tests that need them.
    return sorted(p for p in INVALID_DIR.glob("*.json") if "expected_error" not in p.name)


@pytest.fixture(params=_valid_fixture_paths(), ids=lambda p: p.name)
def valid_fixture_path(request: pytest.FixtureRequest) -> Path:
    return request.param  # type: ignore[no-any-return]


@pytest.fixture(params=_invalid_fixture_paths(), ids=lambda p: p.name)
def invalid_fixture_path(request: pytest.FixtureRequest) -> Path:
    return request.param  # type: ignore[no-any-return]


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def expected_error_for(invalid_path: Path) -> dict[str, str]:
    sibling = invalid_path.with_suffix(".expected_error.json")
    if not sibling.exists():
        raise FileNotFoundError(f"missing expected-error sidecar for {invalid_path.name}")
    return json.loads(sibling.read_text())  # type: ignore[no-any-return]
