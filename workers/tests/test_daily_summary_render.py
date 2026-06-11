"""Daily summary — text rendering (pure, deterministic, no DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from marketmind_workers.observability.models import (
    BotHealth,
    DailySummary,
    EquitySummary,
    StrategySummary,
)
from marketmind_workers.observability.render import render_summary

_GENERATED = datetime(2026, 5, 22, 0, 5, tzinfo=UTC)


def _healthy_summary() -> DailySummary:
    return DailySummary(
        date="2026-05-22",
        generated_at=_GENERATED,
        bot_health=BotHealth(
            status="HEALTHY",
            heartbeat_age_seconds=4.0,
            heartbeat_fresh=True,
            cycles_24h=1438,
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
                name="Golden Cross 50/200 SMA 4H BTC",
                version=1,
                template="ma_trend",
                timeframe="4h",
                symbol="BTC/USDT",
                status="EVALUATING",
                last_decision="HOLD",
                last_cycle_age_hours=0.02,
                bars_have=300,
                bars_needed=205,
            ),
            StrategySummary(
                name="Modern Turtle Donchian Breakout 4H BTC",
                version=1,
                template="spec",
                timeframe="4h",
                symbol="BTC/USDT",
                status="WARMUP",
                bars_have=214,
                bars_needed=255,
            ),
        ],
        notes=["Modern Turtle in warmup — 214/255 bars, first evaluation ~6.8 days out."],
    )


def test_render_happy_path() -> None:
    text = render_summary(_healthy_summary())
    assert text.startswith("=== MarketMind Daily Summary — 2026-05-22 ===")
    assert "Generated: 2026-05-22T00:05:00+00:00" in text
    assert "  Status: HEALTHY" in text
    assert "  Heartbeat: 4s ago (fresh)" in text
    assert "  Current: £1,012.50" in text
    assert "  Change last 24h: +£12.50 (+1.25%)" in text
    assert "  All-time P&L: +£12.50 since 2026-05-20" in text
    assert "Strategies (2 active, 2 total):" in text
    assert "    Status: EVALUATING" in text
    assert "    Bars history: full" in text  # 300/205 → full
    assert "    Bars history: 214/255" in text  # warmup
    assert "    Last cycle: no activity yet" in text  # turtle, no decision
    assert text.endswith("\n")


def test_render_bot_down_banner_is_prominent() -> None:
    summary = _healthy_summary()
    summary.bot_health = BotHealth(
        status="DOWN", heartbeat_age_seconds=21600.0, heartbeat_fresh=False,
    )
    text = render_summary(summary)
    assert "BOT NOT RUNNING" in text
    # The banner must sit above the Bot health section — prominent.
    assert text.index("BOT NOT RUNNING") < text.index("Bot health:")
    assert "6.0h stale" in text


def test_render_zero_strategies() -> None:
    summary = DailySummary(
        date="2026-05-22",
        generated_at=_GENERATED,
        bot_health=BotHealth(status="HEALTHY", heartbeat_fresh=True),
        equity=EquitySummary(),
    )
    text = render_summary(summary)
    assert "Strategies (0 active, 0 total):" in text
    assert "  (none seeded)" in text
    assert "  Current: —" in text  # all-None equity renders cleanly
    assert "  (none)" in text  # no notes


def test_render_mixed_strategy_states() -> None:
    summary = _healthy_summary()
    summary.strategies.append(
        StrategySummary(
            name="Old Disabled Strategy",
            version=2,
            template="breakout",
            timeframe="4h",
            symbol="BTC/USDT",
            status="DISABLED",
        ),
    )
    text = render_summary(summary)
    # 3 strategies, 1 disabled → 2 active.
    assert "Strategies (2 active, 3 total):" in text
    assert "    Status: EVALUATING" in text
    assert "    Status: WARMUP" in text
    assert "    Status: DISABLED" in text


def test_render_is_deterministic() -> None:
    summary = _healthy_summary()
    assert render_summary(summary) == render_summary(summary)


def test_summary_json_round_trips_against_schema() -> None:
    # The JSON file is the source of truth — it must validate as a
    # DailySummary (Pydantic is the schema).
    summary = _healthy_summary()
    payload = json.loads(json.dumps(summary.model_dump(mode="json")))
    assert DailySummary.model_validate(payload) == summary
