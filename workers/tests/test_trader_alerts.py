"""Tests for the trader v1 alert dispatcher.

Layer 1: pure-ish unit tests for `_send_to_telegram` using
`httpx.MockTransport` to short-circuit the network. No real HTTP
call ever fires in the test suite; if you see network egress in
CI for this file, something is wrong.
Layer 2: integration tests for `dispatch_pending_alerts` via
testcontainers Postgres + a `MockTransport`.

The matrix this file pins:
  - log + any severity        ⇒ delivered=True (audit only)
  - telegram + info           ⇒ delivered=True (no network)
  - telegram + warning/critical, creds present, 200 ok:true   ⇒ delivered=True
  - telegram + warning/critical, creds present, 200 ok:false  ⇒ delivered=False
  - telegram + warning/critical, creds present, 5xx           ⇒ delivered=False
  - telegram + warning/critical, creds present, network error ⇒ delivered=False
  - telegram + warning/critical, creds missing                ⇒ delivered=False
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from marketmind_workers.trader.alerts import (
    _send_to_telegram,
    dispatch_pending_alerts,
)
from marketmind_workers.trader.config import TraderSettings, get_trader_settings

# ---- Layer 1: _send_to_telegram (no DB, no real HTTP) ---------------------


class TestSendToTelegram:
    """All cases use `httpx.MockTransport` so no real network call fires."""

    def _ok_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True, "result": {}}),
        )

    def _ok_false_transport(self, description: str) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda req: httpx.Response(
                200,
                json={"ok": False, "description": description},
            ),
        )

    def _http_status_transport(self, status: int) -> httpx.MockTransport:
        return httpx.MockTransport(
            lambda req: httpx.Response(status, text=f"error body {status}"),
        )

    def test_credentials_missing_returns_no_network_call(self) -> None:
        """Empty bot_token ⇒ early-return with a documented error
        string. No HTTP attempt; the test doesn't even pass a
        transport, proving no client is constructed.
        """
        outcome = _send_to_telegram(
            bot_token="",
            chat_id="some-chat",
            subject="s",
            body="b",
        )
        assert outcome.success is False
        assert outcome.error == "telegram credentials not configured"

    def test_chat_id_missing_returns_no_network_call(self) -> None:
        outcome = _send_to_telegram(
            bot_token="token",
            chat_id="",
            subject="s",
            body="b",
        )
        assert outcome.success is False
        assert outcome.error == "telegram credentials not configured"

    def test_success_response_returns_success(self) -> None:
        with httpx.Client(transport=self._ok_transport()) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is True
        assert outcome.error is None

    def test_ok_false_response_returns_failure_with_description(self) -> None:
        with httpx.Client(
            transport=self._ok_false_transport("Bad Request: chat not found"),
        ) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is False
        assert outcome.error is not None
        assert "ok=false" in outcome.error
        assert "chat not found" in outcome.error

    def test_5xx_response_exhausts_all_attempts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """5xx is TRANSIENT — retries up to MAX_ATTEMPTS (3). All
        three fail ⇒ failure with "5xx" in the last_error string.
        time.sleep monkeypatched so the test runs in ms instead
        of 4s.
        """
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            lambda _t: None,
        )
        call_count = 0

        def transport(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(503)

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is False
        assert outcome.error is not None
        assert "5xx" in outcome.error
        # All 3 attempts fired; no more.
        assert call_count == 3

    def test_4xx_response_does_not_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """4xx is PERMANENT (bad token, wrong chat_id, etc.).
        Retrying won't help — return after exactly 1 attempt.
        """
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            lambda _t: None,
        )
        call_count = 0

        def transport(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(401, text="invalid token")

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            outcome = _send_to_telegram(
                bot_token="bad",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is False
        assert outcome.error is not None
        assert "client error" in outcome.error
        # Permanent — no retry.
        assert call_count == 1

    def test_network_error_exhausts_all_attempts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """httpx.ConnectError is TRANSIENT — retries up to 3 attempts."""
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            lambda _t: None,
        )
        call_count = 0

        def transport(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("simulated DNS hiccup")

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is False
        assert outcome.error is not None
        assert "http error" in outcome.error
        assert call_count == 3

    def test_in_call_retry_recovers_after_two_5xx(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Transient: 5xx on attempts 1 and 2, success on attempt 3.

        This is the load-bearing scenario behind the in-call retry —
        a kill_switch alert during a brief Telegram outage must
        deliver within seconds (~4s max), not wait for the next
        4h cycle.
        """
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            lambda _t: None,
        )
        call_count = 0

        def transport(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is True
        assert outcome.error is None
        assert call_count == 3

    def test_in_call_retry_recovers_after_network_blip(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One ConnectError then success — second attempt wins."""
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            lambda _t: None,
        )
        call_count = 0

        def transport(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("blip")
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is True
        assert call_count == 2

    def test_in_call_retry_does_not_fire_on_ok_false(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`{"ok": false}` is PERMANENT (Telegram rejected the
        payload semantically — bad chat_id, etc.). One attempt only.
        """
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            lambda _t: None,
        )
        call_count = 0

        def transport(_req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"ok": False, "description": "chat not found"})

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            outcome = _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        assert outcome.success is False
        assert outcome.error is not None
        assert "chat not found" in outcome.error
        assert call_count == 1

    def test_in_call_retry_sleeps_with_correct_backoffs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verify the backoff schedule: 1s after attempt 1, 3s after
        attempt 2. (No sleep after the final attempt.)
        """
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            "marketmind_workers.trader.alerts.time.sleep",
            sleep_calls.append,
        )

        def transport(_req: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with httpx.Client(transport=httpx.MockTransport(transport)) as client:
            _send_to_telegram(
                bot_token="t",
                chat_id="c",
                subject="hello",
                body="body",
                client=client,
            )
        # Two backoffs between three attempts.
        assert sleep_calls == [1.0, 3.0]

    def test_request_payload_includes_chat_id_and_message(self) -> None:
        """Verify the POST body contains chat_id + the joined
        subject/body text. The Telegram bot rejects calls without
        either; we want a regression to surface fast.
        """
        captured: dict[str, Any] = {}

        def capture(request: httpx.Request) -> httpx.Response:
            import json

            captured["url"] = str(request.url)
            captured["payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json={"ok": True})

        with httpx.Client(transport=httpx.MockTransport(capture)) as client:
            _send_to_telegram(
                bot_token="THE-TOKEN",
                chat_id="42",
                subject="Critical: kill switch",
                body="Drawdown 12% over 30d, max threshold 10%",
                client=client,
            )

        assert "THE-TOKEN" in captured["url"]
        assert "sendMessage" in captured["url"]
        assert captured["payload"]["chat_id"] == "42"
        assert "Critical: kill switch" in captured["payload"]["text"]
        assert "Drawdown 12%" in captured["payload"]["text"]


# ---- Layer 2: integration tests for the orchestrator ----------------------


pytestmark_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_container() -> Iterator[object]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: object) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def _clean(database_url: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE trader_alerts RESTART IDENTITY")
        conn.commit()


@pytest.fixture
def settings_with_creds(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_trader_settings.cache_clear()
    return get_trader_settings()


@pytest.fixture
def settings_no_creds(monkeypatch: pytest.MonkeyPatch) -> TraderSettings:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    get_trader_settings.cache_clear()
    return get_trader_settings()


def _seed_alert(
    database_url: str,
    *,
    channel: str,
    severity: str,
    subject: str = "test alert",
    body: str = "test body",
) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_alerts (channel, severity, subject, body, delivered)
            VALUES (%s, %s, %s, %s, FALSE)
            """,
            (channel, severity, subject, body),
        )
        conn.commit()


# ---- Integration tests -----------------------------------------------------


@pytestmark_integration
def test_log_channel_alerts_marked_delivered_without_network(
    database_url: str,
    settings_with_creds: TraderSettings,
    _clean: None,
) -> None:
    """`channel='log'` ⇒ audit only. delivered flips True; no HTTP
    call attempted. We pass a no-op client that would ERROR if the
    dispatcher tried to use it for log alerts.
    """

    def err(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("dispatcher attempted a network call for a log alert")

    _seed_alert(database_url, channel="log", severity="warning")
    with httpx.Client(transport=httpx.MockTransport(err)) as client:
        result = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client,
        )
    assert result.alerts_skipped_no_dispatch == 1
    assert result.alerts_attempted_telegram == 0


@pytestmark_integration
def test_info_severity_via_telegram_skips_network(
    database_url: str,
    settings_with_creds: TraderSettings,
    _clean: None,
) -> None:
    """`(telegram, info)` is no-network too — the policy keeps
    Telegram for warning/critical only. Same strict
    transport-raises-if-invoked pattern as the log-channel test,
    so a routing-policy regression that wrongly dispatches an
    info alert blows up loudly in CI.
    """

    def err(_req: httpx.Request) -> httpx.Response:
        raise AssertionError("dispatcher attempted to dispatch an info-severity alert")

    _seed_alert(database_url, channel="telegram", severity="info")
    with httpx.Client(transport=httpx.MockTransport(err)) as client:
        result = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client,
        )
    assert result.alerts_skipped_no_dispatch == 1
    assert result.alerts_attempted_telegram == 0
    assert result.alerts_delivered == 0
    assert result.alerts_failed == 0

    # Row marked delivered=True (audit-only path).
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT delivered, delivery_error FROM trader_alerts LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        delivered, error = row
        assert delivered is True
        assert error is None


@pytestmark_integration
def test_warning_telegram_dispatched_and_marked_delivered(
    database_url: str,
    settings_with_creds: TraderSettings,
    _clean: None,
) -> None:
    _seed_alert(
        database_url,
        channel="telegram",
        severity="warning",
        subject="Drift breach",
        body="Strategy X has drifted past threshold",
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True}),
        ),
    ) as client:
        result = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client,
        )
    assert result.alerts_attempted_telegram == 1
    assert result.alerts_delivered == 1
    assert result.alerts_failed == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT delivered FROM trader_alerts LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True


@pytestmark_integration
def test_critical_telegram_failure_stays_undelivered_with_error(
    database_url: str,
    settings_with_creds: TraderSettings,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram 5xx ⇒ delivered=False, delivery_error populated.
    Next cycle will retry. In-call retry behaviour is exhaustively
    covered by `TestSendToTelegram`; this test only cares about
    the post-exhaustion DB state, so sleep is patched out.
    """
    monkeypatch.setattr("marketmind_workers.trader.alerts.time.sleep", lambda _t: None)
    _seed_alert(database_url, channel="telegram", severity="critical")
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(503, text="service unavailable"),
        ),
    ) as client:
        result = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client,
        )
    assert result.alerts_failed == 1
    assert result.alerts_delivered == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT delivered, delivery_error FROM trader_alerts LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        delivered, error = row
        assert delivered is False
        assert error is not None
        assert "5xx" in error


@pytestmark_integration
def test_missing_credentials_marks_failure_not_delivered(
    database_url: str,
    settings_no_creds: TraderSettings,
    _clean: None,
) -> None:
    """Without credentials, the dispatcher records the
    "telegram credentials not configured" error on the row but
    leaves delivered=False. The next cycle (once creds are set)
    will deliver.
    """
    _seed_alert(database_url, channel="telegram", severity="warning")
    result = dispatch_pending_alerts(database_url, settings_no_creds)
    assert result.alerts_failed == 1
    assert result.alerts_delivered == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT delivered, delivery_error FROM trader_alerts LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        delivered, error = row
        assert delivered is False
        assert error is not None
        assert "credentials not configured" in error


@pytestmark_integration
def test_failed_alert_retries_on_next_cycle_and_succeeds(
    database_url: str,
    settings_with_creds: TraderSettings,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First cycle: Telegram returns 5xx (in-call retry exhausts) → failed.
    Second cycle: same row retried with healthy Telegram → delivered.

    This is the cross-cycle retry contract — a row that exhausted
    in-call retries stays pending until the next dispatcher run.
    """
    monkeypatch.setattr("marketmind_workers.trader.alerts.time.sleep", lambda _t: None)
    _seed_alert(database_url, channel="telegram", severity="warning")

    # First cycle: failure.
    with httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(503)),
    ) as client_fail:
        first = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client_fail,
        )
    assert first.alerts_failed == 1

    # Second cycle: success.
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True}),
        ),
    ) as client_ok:
        second = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client_ok,
        )
    assert second.alerts_loaded == 1  # the same row is still pending
    assert second.alerts_delivered == 1

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT delivered, delivery_error FROM trader_alerts LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        delivered, error = row
        assert delivered is True
        assert error is None  # cleared on success


@pytestmark_integration
def test_dispatcher_processes_alerts_oldest_first(
    database_url: str,
    settings_with_creds: TraderSettings,
    _clean: None,
) -> None:
    """Backlog: three pending alerts dispatched in ts-ascending
    order. We can't easily inspect the order from outside
    (Telegram mock fires three times); the smoke test is just
    that all three deliver in one cycle.
    """
    for i in range(3):
        _seed_alert(
            database_url,
            channel="telegram",
            severity="warning",
            subject=f"alert {i}",
            body="body",
        )

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True}),
        ),
    ) as client:
        result = dispatch_pending_alerts(
            database_url,
            settings_with_creds,
            http_client=client,
        )
    assert result.alerts_loaded == 3
    assert result.alerts_delivered == 3
