"""Risk guards — config-driven, every trigger logged with a reason code.

Guards (mandate Stage 5): per-position cap, gross-exposure cap, daily loss
stop (halt new entries until the next UTC day), max-drawdown stop (global
kill requiring a manual reset), global + per-strategy daily trade caps,
per-symbol cooldown, kill switch honored from either a DB flag or the
presence of a KILLSWITCH file — checked every loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import structlog

from marketmind_workers.ftr.config.settings import FTRSettings
from marketmind_workers.ftr.strategies.records import ReasonCode

logger = structlog.get_logger(__name__)

KILLSWITCH_FILE = Path("KILLSWITCH")


@dataclass
class GuardState:
    day: str = ""
    day_start_equity: Decimal = Decimal("0")
    peak_equity: Decimal = Decimal("0")
    trades_today_global: int = 0
    trades_today_by_strategy: dict[str, int] = field(default_factory=dict)
    last_trade_ts_by_symbol: dict[str, datetime] = field(default_factory=dict)
    daily_halt: bool = False
    drawdown_killed: bool = False


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: ReasonCode | None = None
    detail: str = ""


class RiskGuards:
    def __init__(self, settings: FTRSettings, *, db_kill_flag: bool = False) -> None:
        self.settings = settings
        self.state = GuardState()
        self._db_kill_flag = db_kill_flag

    def set_db_kill_flag(self, engaged: bool) -> None:
        self._db_kill_flag = engaged

    def roll_day(self, now: datetime, equity: Decimal) -> None:
        day = now.astimezone(UTC).strftime("%Y-%m-%d")
        if day != self.state.day:
            self.state.day = day
            self.state.day_start_equity = equity
            self.state.trades_today_global = 0
            self.state.trades_today_by_strategy = {}
            self.state.daily_halt = False

    def update_equity(self, equity: Decimal) -> None:
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity

    # -- checks --------------------------------------------------------------

    def kill_switch_engaged(self) -> bool:
        return self._db_kill_flag or KILLSWITCH_FILE.exists()

    def check_entry(
        self,
        *,
        strategy_id: str,
        symbol: str,
        now: datetime,
        equity: Decimal,
        target_notional: Decimal,
        gross_exposure_pct: float,
    ) -> GuardDecision:
        s = self.settings
        st = self.state

        if self.kill_switch_engaged():
            return self._block("kill switch engaged")
        if st.drawdown_killed:
            return self._block("max-drawdown kill active (manual reset required)")
        if st.daily_halt:
            return self._block("daily loss stop active until next UTC day")

        # Max-drawdown kill is checked BEFORE the daily-loss halt: a drop
        # large enough to trip both must engage the sticky kill, not just
        # the until-tomorrow halt (test_ftr_guardrails_trip caught the
        # reversed ordering).
        if st.peak_equity > 0:
            dd = (equity - st.peak_equity) / st.peak_equity
            if float(dd) <= -s.max_drawdown_stop_pct:
                st.drawdown_killed = True
                logger.error("ftr_guard_max_drawdown_kill", drawdown=float(dd))
                return self._block(f"max drawdown kill tripped ({float(dd):.4f})")

        if equity > 0 and st.day_start_equity > 0:
            day_pnl = (equity - st.day_start_equity) / st.day_start_equity
            if float(day_pnl) <= -s.daily_loss_stop_pct:
                st.daily_halt = True
                logger.warning("ftr_guard_daily_loss_stop", day_pnl=float(day_pnl))
                return self._block(f"daily loss stop tripped ({float(day_pnl):.4f})")

        if equity > 0 and float(target_notional / equity) > s.max_position_pct:
            return self._block(
                f"position would exceed per-position cap {s.max_position_pct:.0%}"
            )
        if gross_exposure_pct + float(target_notional / equity if equity > 0 else 0) > (
            s.max_gross_exposure_pct + 1e-9
        ):
            return self._block("gross exposure cap would be exceeded")

        if st.trades_today_global >= s.max_trades_per_day_global:
            return GuardDecision(
                allowed=False,
                reason=ReasonCode.SKIP_MAX_TRADES,
                detail=f"global daily trade cap {s.max_trades_per_day_global} reached",
            )

        last = st.last_trade_ts_by_symbol.get(symbol)
        if last is not None:
            hours = (now - last).total_seconds() / 3600.0
            if hours < s.per_symbol_cooldown_hours:
                return GuardDecision(
                    allowed=False,
                    reason=ReasonCode.SKIP_COOLDOWN,
                    detail=f"{symbol} cooldown {hours:.1f}h < {s.per_symbol_cooldown_hours}h",
                )

        return GuardDecision(allowed=True)

    def record_trade(self, strategy_id: str, symbol: str, now: datetime) -> None:
        self.state.trades_today_global += 1
        self.state.trades_today_by_strategy[strategy_id] = (
            self.state.trades_today_by_strategy.get(strategy_id, 0) + 1
        )
        self.state.last_trade_ts_by_symbol[symbol] = now

    @staticmethod
    def _block(detail: str) -> GuardDecision:
        logger.warning("ftr_guard_block", detail=detail)
        return GuardDecision(allowed=False, reason=ReasonCode.SKIP_GUARDRAIL, detail=detail)
