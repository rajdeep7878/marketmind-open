from __future__ import annotations

from datetime import UTC, datetime

from marketmind_workers.jobs.dummy import run


def test_dummy_run_echoes_message() -> None:
    result = run(message="hello")
    assert result["echoed"] == "hello"
    # completed_at should parse as a UTC ISO timestamp
    parsed = datetime.fromisoformat(result["completed_at"])
    assert parsed.tzinfo is not None
    assert parsed.tzinfo.utcoffset(parsed) == UTC.utcoffset(parsed)


def test_dummy_run_returns_only_expected_keys() -> None:
    # Locks the contract — any new key here would be a behavior change
    # the API serializer needs to know about.
    result = run(message="x")
    assert set(result.keys()) == {"echoed", "completed_at"}
