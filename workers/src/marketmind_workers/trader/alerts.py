"""Trader v1 alert dispatcher.

The dispatcher pulls undelivered `trader_alerts` rows and routes
each one according to its `(channel, severity)`:

  - `(telegram, info)` or `channel='log'` ⇒ mark `delivered=True`
    without making a network call. The DB row IS the audit trail
    for `info`-level events; Telegram noise from informational
    pings would train operators to ignore the channel.

  - `(telegram, warning)` or `(telegram, critical)` ⇒ POST to
    Telegram's `sendMessage` endpoint. On success, set
    `delivered=True`. On failure (network error, 5xx, or
    `{"ok": false}` response), leave `delivered=False` and store
    the error string in `delivery_error` so the next cycle
    retries.

CREDENTIALS HANDLING
====================
Telegram needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. When
either is empty / unset, the dispatcher:
  - Leaves the alert row `delivered=False` (so once the operator
    configures credentials, the backlog flushes on the next
    cycle).
  - Sets `delivery_error = "telegram credentials not configured"`
    so the operator can SELECT the row count and see "N alerts
    waiting on credentials".

This is the "always write the alert row even if Telegram fails"
guarantee from the prompt: the row is the source of truth; the
network call is best-effort retry.

TWO-TIER RETRY POLICY (load-bearing — belt and braces)
======================================================
Transient failures (brief network blip, Telegram 503, single
rate-limit hit) almost always succeed seconds later. Waiting a
full 4h signal-execution cycle to redeliver a `kill_switch tripped`
alert is unacceptable. Persistent failures (revoked token, wrong
chat_id, region block) won't be fixed by hammering — better to
wait for the operator.

In-call retry handles the transient case:
  3 attempts on transient failures (httpx.NetworkError /
  httpx.ConnectError / 5xx response) with backoff (1s, 3s).
  Total bounded wait ≤ 4s per failed alert. Permanent failures
  (4xx, `{"ok": false}` with a Telegram error description) do
  NOT retry — the operator needs to fix and the next cycle
  picks it up.

Cross-cycle retry handles the persistent case:
  A row that exhausted in-call retries stays `delivered=False`
  and gets retried on every subsequent dispatcher cycle until
  success. No retry-count cap in v1: operator visibility is
  paramount, and a persistent failure ("Telegram broken for 6h")
  is itself the signal. Quietly giving up after N attempts would
  mask the outage.

Belt and braces: ~4s of in-call retry handles 99% of failures
without delaying the cycle meaningfully; cross-cycle retry is
the safety net for genuine outages.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

import httpx
import psycopg
import structlog
from marketmind_shared.schemas.strategy_spec.common import _StrictModel

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.heartbeat import touch_heartbeat

log = structlog.get_logger(__name__)


_TELEGRAM_BASE_URL: Final[str] = "https://api.telegram.org"
_TELEGRAM_TIMEOUT_S: Final[float] = 10.0

# In-call retry policy. Three attempts total. Backoffs between
# attempts are 1s, then 3s (between attempts 1→2 and 2→3
# respectively). Total max wait per failed alert ≤ 4s — bounded,
# doesn't meaningfully block the cycle.
_TELEGRAM_MAX_ATTEMPTS: Final[int] = 3
_TELEGRAM_RETRY_BACKOFFS_S: Final[tuple[float, ...]] = (1.0, 3.0)


# ---- Pure-ish HTTP helper --------------------------------------------------


@dataclass(frozen=True)
class _DeliveryOutcome:
    success: bool
    error: str | None = None


def _send_to_telegram(
    *,
    bot_token: str,
    chat_id: str,
    subject: str,
    body: str,
    client: httpx.Client | None = None,
) -> _DeliveryOutcome:
    """POST one message to Telegram's `sendMessage` endpoint, with
    in-call retry on transient failures.

    Returns `_DeliveryOutcome(success=True)` if Telegram replied
    with `{"ok": true, ...}`; otherwise returns
    `_DeliveryOutcome(success=False, error="...")` with the
    last error string ready for `trader_alerts.delivery_error`.

    Retry classification:
      - 5xx response       → TRANSIENT, retry up to 3 attempts
      - httpx.HTTPError    → TRANSIENT, retry up to 3 attempts
      - 4xx response       → PERMANENT, no retry (bad token/chat)
      - `{"ok": false}`    → PERMANENT, no retry (Telegram rejected payload)
      - Non-JSON body      → PERMANENT, no retry (broken endpoint?)

    `client` is injectable for testing — the test suite passes
    `httpx.Client(transport=httpx.MockTransport(...))` to short-
    circuit the network.
    """
    if not bot_token or not chat_id:
        return _DeliveryOutcome(
            success=False,
            error="telegram credentials not configured",
        )

    text = f"*{subject}*\n\n{body}"
    url = f"{_TELEGRAM_BASE_URL}/bot{bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }

    owns_client = client is None
    actual_client = client or httpx.Client(timeout=_TELEGRAM_TIMEOUT_S)
    try:
        last_error = "no attempts made"
        for attempt in range(_TELEGRAM_MAX_ATTEMPTS):
            # ---- attempt the POST ----
            try:
                response = actual_client.post(url, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"http error: {exc}"
                _sleep_between_attempts(attempt)
                continue

            # ---- classify the response ----
            if response.status_code >= 500:
                last_error = f"telegram 5xx (status {response.status_code})"
                _sleep_between_attempts(attempt)
                continue
            if response.status_code >= 400:
                # PERMANENT — wrong token, bad chat_id, etc.
                return _DeliveryOutcome(
                    success=False,
                    error=(
                        f"telegram client error (status {response.status_code}): "
                        f"{response.text[:200]}"
                    ),
                )
            try:
                body_json = response.json()
            except ValueError:
                # PERMANENT — endpoint returning non-JSON is a real outage,
                # not a flake. Don't waste retries.
                return _DeliveryOutcome(
                    success=False,
                    error="telegram returned non-JSON body",
                )
            if not body_json.get("ok", False):
                # PERMANENT — Telegram rejected the payload semantically.
                description = body_json.get("description", "unknown")
                return _DeliveryOutcome(
                    success=False,
                    error=f"telegram returned ok=false: {description}",
                )
            return _DeliveryOutcome(success=True)

        # Exhausted in-call retries; cross-cycle retry takes over.
        return _DeliveryOutcome(success=False, error=last_error)
    finally:
        if owns_client:
            actual_client.close()


def _sleep_between_attempts(completed_attempt: int) -> None:
    """Sleep before the next attempt; no-op after the final attempt.

    `completed_attempt` is the index of the attempt that just
    failed (0 = first attempt). Sleeps `_TELEGRAM_RETRY_BACKOFFS_S[
    completed_attempt]` — i.e., 1s after attempt 0, 3s after attempt
    1. Doesn't fire after the last attempt because we're about to
    return failure anyway.
    """
    if completed_attempt < len(_TELEGRAM_RETRY_BACKOFFS_S):
        time.sleep(_TELEGRAM_RETRY_BACKOFFS_S[completed_attempt])


# ---- Result type -----------------------------------------------------------


class AlertDispatchResult(_StrictModel):
    """Aggregate stats from one dispatcher pass."""

    alerts_loaded: int = 0
    alerts_skipped_no_dispatch: int = 0  # severity=info, channel=log, etc.
    alerts_attempted_telegram: int = 0
    alerts_delivered: int = 0
    alerts_failed: int = 0


@dataclass
class _CycleState:
    alerts_loaded: int = 0
    alerts_skipped_no_dispatch: int = 0
    alerts_attempted_telegram: int = 0
    alerts_delivered: int = 0
    alerts_failed: int = 0


# ---- DB-touching helpers ---------------------------------------------------


def _load_pending_alerts(
    conn: psycopg.Connection[Any],
) -> list[tuple[UUID, str, str, str, str]]:
    """Return `(id, channel, severity, subject, body)` for every
    undelivered alert, oldest first.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, channel, severity, subject, body
            FROM trader_alerts
            WHERE delivered = FALSE
            ORDER BY ts ASC
            """,
        )
        rows = cur.fetchall()
    return [(UUID(str(r[0])), r[1], r[2], r[3], r[4]) for r in rows]


def _mark_delivered(conn: psycopg.Connection[Any], alert_id: UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trader_alerts
            SET delivered = TRUE, delivery_error = NULL
            WHERE id = %s
            """,
            (str(alert_id),),
        )


def _mark_failed(
    conn: psycopg.Connection[Any],
    alert_id: UUID,
    error: str,
) -> None:
    """Leave `delivered=False` so the next cycle retries; record
    the error string. Truncated to 500 chars so a verbose
    traceback can't blow out the row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE trader_alerts
            SET delivery_error = %s
            WHERE id = %s
            """,
            (error[:500], str(alert_id)),
        )


# ---- Public orchestrator ---------------------------------------------------


def dispatch_pending_alerts(
    database_url: str,
    settings: TraderSettings,
    *,
    http_client: httpx.Client | None = None,
    run_id: UUID | None = None,
) -> AlertDispatchResult:
    """Pull undelivered alerts; dispatch per severity policy.

    Routing matrix:
      | channel  | severity         | action                              |
      |----------|------------------|-------------------------------------|
      | log      | any              | mark delivered=True (audit only)    |
      | telegram | info             | mark delivered=True (no network)    |
      | telegram | warning/critical | POST to Telegram, mark on success   |

    `http_client` is injectable for tests. Production paths pass
    `None` and the function constructs a fresh client per cycle.
    """
    state = _CycleState()
    bot_token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    with psycopg.connect(database_url) as conn:
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="alerts")
        alerts = _load_pending_alerts(conn)
        state.alerts_loaded = len(alerts)
        log.info("alert_dispatch_starting", pending=len(alerts))

        for alert_id, channel, severity, subject, body in alerts:
            # No-dispatch paths: log channel + info severity stay
            # in the DB as audit only.
            if channel == "log" or severity == "info":
                with conn.transaction():
                    _mark_delivered(conn, alert_id)
                state.alerts_skipped_no_dispatch += 1
                continue

            # Telegram path for warning + critical.
            state.alerts_attempted_telegram += 1
            outcome = _send_to_telegram(
                bot_token=bot_token,
                chat_id=chat_id,
                subject=subject,
                body=body,
                client=http_client,
            )
            with conn.transaction():
                if outcome.success:
                    _mark_delivered(conn, alert_id)
                    state.alerts_delivered += 1
                else:
                    error = outcome.error or "unknown delivery error"
                    _mark_failed(conn, alert_id, error)
                    state.alerts_failed += 1
                    log.warning(
                        "alert_delivery_failed",
                        alert_id=str(alert_id),
                        severity=severity,
                        error=error,
                    )

    result = AlertDispatchResult(
        alerts_loaded=state.alerts_loaded,
        alerts_skipped_no_dispatch=state.alerts_skipped_no_dispatch,
        alerts_attempted_telegram=state.alerts_attempted_telegram,
        alerts_delivered=state.alerts_delivered,
        alerts_failed=state.alerts_failed,
    )
    log.info("alert_dispatch_complete", **result.model_dump())
    return result


__all__ = ["AlertDispatchResult", "dispatch_pending_alerts"]
