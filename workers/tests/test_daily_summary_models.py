"""Daily summary — data-model round-trip + JSON-schema validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from marketmind_workers.observability.models import (
    SCHEMA_VERSION,
    BotHealth,
    DailySummary,
    EquitySummary,
    StrategySummary,
)


def _minimal_summary() -> DailySummary:
    return DailySummary(
        date="2026-05-22",
        generated_at=datetime(2026, 5, 22, 0, 5, tzinfo=UTC),
        bot_health=BotHealth(status="HEALTHY", heartbeat_fresh=True),
        equity=EquitySummary(),
    )


def test_minimal_summary_round_trips() -> None:
    summary = _minimal_summary()
    restored = DailySummary.model_validate(
        json.loads(json.dumps(summary.model_dump(mode="json"))),
    )
    assert restored == summary
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.strategies == []


def test_full_summary_round_trips() -> None:
    summary = DailySummary(
        date="2026-05-22",
        generated_at=datetime(2026, 5, 22, 0, 5, tzinfo=UTC),
        bot_health=BotHealth(
            status="HEALTHY",
            heartbeat_age_seconds=3.2,
            heartbeat_fresh=True,
            cycles_24h=1440,
            signal_cycles_24h=6,
        ),
        equity=EquitySummary(
            current_gbp=1012.50,
            change_24h_gbp=12.50,
            change_24h_pct=1.25,
            open_positions=1,
            closed_trades_24h=2,
            all_time_pnl_gbp=12.50,
            all_time_since="2026-05-20",
        ),
        strategies=[
            StrategySummary(
                name="Modern Turtle Donchian Breakout 4H BTC",
                version=1,
                template="spec",
                timeframe="4h",
                symbol="BTC/USDT",
                status="WARMUP",
                last_decision=None,
                bars_have=214,
                bars_needed=255,
                state_rows=0,
                trades_24h=0,
            ),
        ],
        risk_events_24h=0,
        notes=["Modern Turtle in warmup — 214/255 bars, first eval ~7 days out."],
    )
    restored = DailySummary.model_validate(
        json.loads(json.dumps(summary.model_dump(mode="json"))),
    )
    assert restored == summary
    assert restored.strategies[0].status == "WARMUP"


def test_summary_json_schema_is_valid_and_self_describing() -> None:
    # The JSON Schema is what tests validate generated reports against.
    schema = DailySummary.model_json_schema()
    assert schema["type"] == "object"
    assert "bot_health" in schema["properties"]
    assert "strategies" in schema["properties"]
    # extra=forbid → unknown keys are rejected on validation.
    assert schema.get("additionalProperties") is False
