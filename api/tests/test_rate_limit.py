"""Tests for the /content/ingest rate limiter + daily cost cap.

Strategy: drive the API via TestClient with the existing fakeredis
fixture. The rate-limit / cost-cap code paths only touch Redis (not
Postgres, not the queue), so the fake is a complete substitute.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
from marketmind_api.config import Settings, get_settings
from marketmind_api.rate_limit import (
    DailyCostCapReached,
    RateLimitExceeded,
    check_daily_cost_cap,
    consume_ingest_quota,
)
from marketmind_shared.rate_limits import daily_cost_key, ingest_rate_limit_key

# ---- unit-level: the pure Redis helpers ----------------------------------


def test_consume_ingest_quota_returns_remaining(fake_redis: FakeRedis) -> None:
    # limit=3 → after first call 2 remain, then 1, then 0; 4th call raises.
    assert consume_ingest_quota(fake_redis, "1.1.1.1", limit=3) == 2
    assert consume_ingest_quota(fake_redis, "1.1.1.1", limit=3) == 1
    assert consume_ingest_quota(fake_redis, "1.1.1.1", limit=3) == 0
    with pytest.raises(RateLimitExceeded):
        consume_ingest_quota(fake_redis, "1.1.1.1", limit=3)


def test_consume_ingest_quota_isolates_by_ip(fake_redis: FakeRedis) -> None:
    # Two IPs each get their own counter.
    consume_ingest_quota(fake_redis, "1.1.1.1", limit=2)
    consume_ingest_quota(fake_redis, "1.1.1.1", limit=2)
    # Same IP is exhausted, but a fresh IP starts at zero.
    assert consume_ingest_quota(fake_redis, "2.2.2.2", limit=2) == 1


def test_consume_ingest_quota_sets_ttl(fake_redis: FakeRedis) -> None:
    consume_ingest_quota(fake_redis, "1.1.1.1", limit=5)
    ttl = fake_redis.ttl(ingest_rate_limit_key("1.1.1.1"))
    # Concrete int per redis-py contract. Allow a small clock-window slack.
    assert isinstance(ttl, int)
    assert 86_300 < ttl <= 86_400


def test_consume_ingest_quota_disabled_when_limit_zero(fake_redis: FakeRedis) -> None:
    """limit=0 (or any non-positive value) disables the guard:
    no Redis write, no rejection, and the returned remaining is -1
    so the X-RateLimit-Remaining header is a sentinel rather than a
    misleading countdown. Mirrors the cap_gbp=0 escape hatch on
    check_daily_cost_cap.
    """
    for _ in range(10):
        assert consume_ingest_quota(fake_redis, "1.1.1.1", limit=0) == -1
    # No counter key was created — the guard never touched Redis.
    assert fake_redis.exists(ingest_rate_limit_key("1.1.1.1")) == 0
    # Negative values are likewise treated as disabled.
    assert consume_ingest_quota(fake_redis, "2.2.2.2", limit=-1) == -1


def test_check_daily_cost_cap_allows_when_under(fake_redis: FakeRedis) -> None:
    # Cap = £5 * 1.27 = $6.35 = 635 cents. We seed 100 cents.
    fake_redis.set(daily_cost_key(), b"100")
    current, cap = check_daily_cost_cap(fake_redis, cap_gbp=5.0, gbp_usd_rate=1.27)
    assert current == 100
    assert cap == 635


def test_check_daily_cost_cap_raises_at_or_above(fake_redis: FakeRedis) -> None:
    # Cap = 635 cents; seed = 635 → raise.
    fake_redis.set(daily_cost_key(), b"635")
    with pytest.raises(DailyCostCapReached):
        check_daily_cost_cap(fake_redis, cap_gbp=5.0, gbp_usd_rate=1.27)


def test_check_daily_cost_cap_disabled_when_cap_zero(fake_redis: FakeRedis) -> None:
    # cap_gbp=0 disables the cap; even with spend recorded we don't raise.
    fake_redis.set(daily_cost_key(), b"99999")
    current, cap = check_daily_cost_cap(fake_redis, cap_gbp=0.0, gbp_usd_rate=1.27)
    assert cap == 0
    assert current == 99_999


# ---- key helper edge cases ----------------------------------------------


def test_daily_cost_key_requires_aware_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        daily_cost_key(datetime(2026, 5, 16, 12, 0, 0))  # noqa: DTZ001 — intentional


def test_daily_cost_key_uses_utc_day() -> None:
    # 23:30 in PDT (UTC-7) is 06:30 UTC the next day.
    from datetime import timedelta, timezone

    pdt = timezone(timedelta(hours=-7))
    night_in_pdt = datetime(2026, 5, 16, 23, 30, 0, tzinfo=pdt)
    assert daily_cost_key(night_in_pdt).endswith("2026-05-17")
    # And a UTC-equivalent value returns the same key.
    same_in_utc = night_in_pdt.astimezone(UTC)
    assert daily_cost_key(same_in_utc) == daily_cost_key(night_in_pdt)


# ---- end-to-end: TestClient + dependency overrides -----------------------


@pytest.fixture
def small_limit_client(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Same TestClient but with the per-IP limit lowered to 3.

    Settings are cached via lru_cache; we monkeypatch the getter to
    return a fresh Settings with the lower limit so each test starts
    from a known threshold without thrashing real env vars.
    """
    from marketmind_api import rate_limit as rate_limit_mod

    base = get_settings()

    def _settings_with_small_limit() -> Settings:
        return base.model_copy(
            update={
                "rate_limit_ingest_per_day": 3,
                # Cap large enough to not trigger in these tests.
                "daily_cost_cap_gbp": 100.0,
            },
        )

    # The Settings dep is resolved fresh per request; overriding via
    # FastAPI's dependency_overrides on the actual dep callable.
    from marketmind_api.config import get_settings as get_settings_dep

    client.app.dependency_overrides[get_settings_dep] = _settings_with_small_limit
    # The rate-limit module imports SettingsDep which uses the same
    # get_settings symbol — overrides applied above flow through.
    _ = rate_limit_mod  # silence unused
    return client


def test_post_ingest_returns_rate_limit_header(
    client: TestClient,
) -> None:
    resp = client.post("/content/ingest", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert resp.status_code == 202, resp.text
    assert resp.headers.get("X-RateLimit-Remaining") == "4"


def test_post_ingest_returns_429_after_quota(
    small_limit_client: TestClient,
) -> None:
    payload = {"url": "https://youtu.be/dQw4w9WgXcQ"}
    for expected_remaining in (2, 1, 0):
        resp = small_limit_client.post("/content/ingest", json=payload)
        assert resp.status_code == 202, resp.text
        assert resp.headers.get("X-RateLimit-Remaining") == str(expected_remaining)

    # 4th request → 429, no enqueue.
    resp = small_limit_client.post("/content/ingest", json=payload)
    assert resp.status_code == 429
    body: dict[str, Any] = resp.json()
    assert body["detail"]["error"] == "rate_limit"
    assert "today's free limit (3 analyses)" in body["detail"]["message"]
    assert resp.headers.get("X-RateLimit-Remaining") == "0"


def test_post_ingest_returns_503_when_cap_reached(
    client: TestClient,
    fake_redis: FakeRedis,
) -> None:
    # Seed the day's cost over the cap so the next request trips it.
    # Default cap is 5 GBP * 1.27 = 635 cents.
    fake_redis.set(daily_cost_key(), b"700")

    resp = client.post("/content/ingest", json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["error"] == "daily_cap_reached"
    assert "midnight UTC" in body["detail"]["message"]


def test_client_ip_prefers_xff(client: TestClient) -> None:
    # Two IPs in XFF — the first is the originating client; both
    # should each get their own counter.
    headers_a = {"X-Forwarded-For": "10.0.0.1, 192.168.1.1"}
    headers_b = {"X-Forwarded-For": "10.0.0.2, 192.168.1.1"}
    for _ in range(5):
        assert (
            client.post(
                "/content/ingest",
                json={"url": "https://youtu.be/dQw4w9WgXcQ"},
                headers=headers_a,
            ).status_code
            == 202
        )
    # A's quota is now used; B is fresh.
    over = client.post(
        "/content/ingest",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=headers_a,
    )
    assert over.status_code == 429
    fresh = client.post(
        "/content/ingest",
        json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        headers=headers_b,
    )
    assert fresh.status_code == 202
