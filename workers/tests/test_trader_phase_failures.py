"""Tests for the per-phase consecutive-failure tracker.

Verifies the state-transition semantics implemented in
`workers.trader.jobs._record_phase_outcome`:

  - 3 consecutive failures of the same phase → critical alert
    written to `trader_alerts` (delivered=False; the next cycle's
    dispatch_pending_alerts ships it).
  - 4th and 5th failures of the same phase → no additional alert
    (suppression).
  - First success after a streak >= 3 → info recovery alert.
  - Sub-threshold streak followed by success → no alert at all.
  - Mixed phases — failures in one phase do NOT affect another's
    counter.

Uses fakeredis (atomic INCR / DELETE / EXPIRE all supported).
DB writes are routed through a test double that captures the
inserts in-memory rather than hitting Postgres (we already cover
the DB path elsewhere).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fakeredis import FakeRedis
from marketmind_shared.schemas.trader import AlertChannel, Severity
from marketmind_workers.trader import jobs as jobs_module


@dataclass
class _CapturedAlert:
    severity: Severity
    channel: AlertChannel
    subject: str
    body: str
    context: dict[str, Any] = field(default_factory=dict)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[_CapturedAlert]:
    """Replace `_emit_alert` with an in-memory recorder. The DB
    insert path is exercised by other tests; here we only care
    about whether/what gets emitted.
    """
    bucket: list[_CapturedAlert] = []

    def _fake_emit(
        _database_url: str,
        *,
        severity: Severity,
        channel: AlertChannel,
        subject: str,
        body: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        bucket.append(
            _CapturedAlert(
                severity=severity,
                channel=channel,
                subject=subject,
                body=body,
                context=context or {},
            ),
        )

    monkeypatch.setattr(jobs_module, "_emit_alert", _fake_emit)
    return bucket


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis(decode_responses=False)


# ---- Threshold trigger -----------------------------------------------------


def test_third_failure_fires_critical_alert(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """The load-bearing test for the threshold edge: counter
    reaches 3 → exactly one critical alert.
    """
    for _ in range(3):
        jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    assert len(captured) == 1
    alert = captured[0]
    assert alert.severity == Severity.CRITICAL
    assert alert.channel == AlertChannel.TELEGRAM
    assert "signal" in alert.subject
    assert alert.context["phase"] == "signal"
    assert alert.context["consecutive_failures"] == 3


def test_first_and_second_failure_do_not_alert(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """Sub-threshold streaks stay silent. Two failures = no alert."""
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    assert captured == []


# ---- Suppression of 4th+ ---------------------------------------------------


def test_fourth_and_fifth_failure_suppress_alert(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """Once the threshold has fired, additional failures within
    the same streak don't emit further alerts.
    """
    for _ in range(5):
        jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    # Only the 3rd failure should have emitted; 4th + 5th silent.
    assert len(captured) == 1
    assert captured[0].context["consecutive_failures"] == 3


# ---- Recovery alert --------------------------------------------------------


def test_recovery_alert_fires_after_threshold_streak(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """First success after a streak >= threshold → recovery info
    alert. The previous_failures count names the streak length.
    """
    for _ in range(4):
        jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    captured.clear()
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=True)
    assert len(captured) == 1
    alert = captured[0]
    assert alert.severity == Severity.INFO
    assert "recovered" in alert.subject.lower()
    assert alert.context["phase"] == "signal"
    assert alert.context["previous_failures"] == 4


def test_no_recovery_if_streak_never_reached_threshold(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """2 failures + 1 success → no alerts of any kind."""
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=True)
    assert captured == []


def test_success_with_no_prior_failures_is_no_op(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """A success on a fresh counter doesn't alert; idempotent for
    the common-case happy path that runs every minute.
    """
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=True)
    assert captured == []


# ---- Counter resets after recovery -----------------------------------------


def test_counter_resets_after_success(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """After a success resets the counter, the next failure
    streak starts over at 1 — it should take 3 more failures
    (not 2) to fire another critical alert.
    """
    # Streak A: 3 failures + 1 success → critical alert + recovery alert.
    for _ in range(3):
        jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=True)
    assert len(captured) == 2  # critical + recovery
    captured.clear()

    # Streak B: 2 failures → no alert (counter is back at 1, 2).
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    assert captured == []
    # 3rd failure → critical alert.
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    assert len(captured) == 1
    assert captured[0].context["consecutive_failures"] == 3


# ---- Per-phase isolation ---------------------------------------------------


def test_phase_counters_are_independent(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """Failures in `signal` should not affect the counter for
    `execute` (or any other phase).
    """
    # 2 failures in signal — should be silent.
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    # 3 failures in execute — should fire a critical alert.
    jobs_module._record_phase_outcome(redis, "db://", "execute", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "execute", success=False)
    jobs_module._record_phase_outcome(redis, "db://", "execute", success=False)
    assert len(captured) == 1
    assert captured[0].context["phase"] == "execute"


# ---- TTL refresh on every increment ---------------------------------------


def test_ttl_set_on_failure(redis: FakeRedis, captured: list[_CapturedAlert]) -> None:
    """After a failure, the counter has a finite TTL. Sanity
    check that the EXPIRE call landed (without this, the counter
    would live forever and any single one-off failure long ago
    would still count toward today's streak).
    """
    _ = captured  # not used here
    jobs_module._record_phase_outcome(redis, "db://", "signal", success=False)
    key = jobs_module._phase_failure_key("signal")
    ttl = redis.ttl(key)
    assert 0 < int(ttl) <= jobs_module._PHASE_FAILURE_TTL_S


# ---- _run_phase end-to-end -------------------------------------------------


def test_run_phase_records_success_for_no_exception(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """`_run_phase` calls fn — no exception ⇒ outcome=success."""

    def _ok() -> None:
        return None

    jobs_module._run_phase(redis, "db://", "signal", _ok)
    assert captured == []
    assert redis.get(jobs_module._phase_failure_key("signal")) is None


def test_run_phase_records_failure_and_alerts_at_threshold(
    redis: FakeRedis, captured: list[_CapturedAlert],
) -> None:
    """Calling `_run_phase` three times where fn raises ⇒
    counter reaches 3 ⇒ critical alert.
    """

    def _bad() -> None:
        raise RuntimeError("boom")

    for _ in range(3):
        jobs_module._run_phase(redis, "db://", "signal", _bad)
    assert len(captured) == 1
    assert captured[0].severity == Severity.CRITICAL
