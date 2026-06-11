"""Daily loss, max DD, max trades/day, cooldown, kill switch — each
triggers and logs correctly (mandate Stage 7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from marketmind_workers.ftr.config.settings import FTRSettings
from marketmind_workers.ftr.strategies.records import ReasonCode
from marketmind_workers.ftr.trader import guards as guards_mod
from marketmind_workers.ftr.trader.guards import RiskGuards

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _guards() -> RiskGuards:
    return RiskGuards(FTRSettings())


def _entry_ok(g: RiskGuards, *, equity: str = "10000", notional: str = "1000") -> bool:
    return g.check_entry(
        strategy_id="s1",
        symbol="BTC/USDT",
        now=_NOW,
        equity=Decimal(equity),
        target_notional=Decimal(notional),
        gross_exposure_pct=0.0,
    ).allowed


def test_daily_loss_stop_trips_and_resets_next_day() -> None:
    g = _guards()
    g.roll_day(_NOW, Decimal("10000"))
    g.update_equity(Decimal("10000"))
    # equity down 2.5% intraday => halt
    decision = g.check_entry(
        strategy_id="s1",
        symbol="BTC/USDT",
        now=_NOW,
        equity=Decimal("9750"),
        target_notional=Decimal("1000"),
        gross_exposure_pct=0.0,
    )
    assert not decision.allowed
    assert decision.reason == ReasonCode.SKIP_GUARDRAIL
    assert g.state.daily_halt
    # next UTC day resets the halt
    tomorrow = _NOW + timedelta(days=1)
    g.roll_day(tomorrow, Decimal("9750"))
    assert not g.state.daily_halt


def test_max_drawdown_kill_requires_manual_reset() -> None:
    g = _guards()
    g.roll_day(_NOW, Decimal("10000"))
    g.update_equity(Decimal("10000"))
    decision = g.check_entry(
        strategy_id="s1",
        symbol="BTC/USDT",
        now=_NOW,
        equity=Decimal("8900"),  # -11% from peak
        target_notional=Decimal("100"),
        gross_exposure_pct=0.0,
    )
    assert not decision.allowed
    assert g.state.drawdown_killed
    # rolling the day does NOT clear the kill
    g.roll_day(_NOW + timedelta(days=1), Decimal("8900"))
    assert g.state.drawdown_killed
    assert not _entry_ok(g, equity="8900")


def test_position_and_gross_caps() -> None:
    g = _guards()
    g.roll_day(_NOW, Decimal("10000"))
    g.update_equity(Decimal("10000"))
    # per-position cap 20%
    assert not _entry_ok(g, notional="2500")
    assert _entry_ok(g, notional="1500")
    # gross cap 100%
    decision = g.check_entry(
        strategy_id="s1",
        symbol="ETH/USDT",
        now=_NOW,
        equity=Decimal("10000"),
        target_notional=Decimal("1500"),
        gross_exposure_pct=0.95,
    )
    assert not decision.allowed


def test_max_trades_per_day_and_cooldown() -> None:
    g = _guards()
    g.roll_day(_NOW, Decimal("10000"))
    g.update_equity(Decimal("10000"))
    for i in range(8):
        g.record_trade("s1", f"SYM{i}/USDT", _NOW)
    decision = g.check_entry(
        strategy_id="s1",
        symbol="NEW/USDT",
        now=_NOW,
        equity=Decimal("10000"),
        target_notional=Decimal("100"),
        gross_exposure_pct=0.0,
    )
    assert not decision.allowed
    assert decision.reason == ReasonCode.SKIP_MAX_TRADES

    g2 = _guards()
    g2.roll_day(_NOW, Decimal("10000"))
    g2.update_equity(Decimal("10000"))
    g2.record_trade("s1", "BTC/USDT", _NOW - timedelta(hours=1))
    decision = g2.check_entry(
        strategy_id="s1",
        symbol="BTC/USDT",
        now=_NOW,
        equity=Decimal("10000"),
        target_notional=Decimal("100"),
        gross_exposure_pct=0.0,
    )
    assert not decision.allowed
    assert decision.reason == ReasonCode.SKIP_COOLDOWN


def test_kill_switch_file_and_db_flag(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    g = _guards()
    g.roll_day(_NOW, Decimal("10000"))
    g.update_equity(Decimal("10000"))
    assert _entry_ok(g)
    # DB flag engages
    g.set_db_kill_flag(True)
    assert g.kill_switch_engaged()
    assert not _entry_ok(g)
    g.set_db_kill_flag(False)
    # KILLSWITCH file engages
    ks = tmp_path / "KILLSWITCH"
    ks.write_text("halt")
    monkeypatch.setattr(guards_mod, "KILLSWITCH_FILE", ks)
    assert g.kill_switch_engaged()
    assert not _entry_ok(g)
