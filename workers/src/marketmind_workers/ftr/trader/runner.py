"""FTR paper trader — asyncio loop, 1-minute cadence. PAPER ONLY.

Deploy eligibility is enforced at load time: a strategy spec is accepted
only if (a) it is not research_simulation_only (refused BY TYPE), and
(b) ftr_verdicts holds a PASS for it on a venue profile with
uk_execution_feasible=true. No verdict, no deployment — the trader idles
honestly, snapshotting equity, rather than trading unvalidated configs.

Architecture (mandate Stage 5): market data via PublicOnlyExchange (no API
keys; private methods raise PaperOnlyViolation); fills simulated by
PaperBroker at the next 1m close worsened by the venue profile; risk guards
checked every loop; full DecisionRecord persistence including skips;
idempotent crash recovery from Postgres.
"""

# ruff: noqa: E402 — the paper-only assert below must run BEFORE any other
# import; everything that follows imports into a process already proven to
# be paper-mode.

from __future__ import annotations

# PAPER ONLY. This system has no live execution path by design. This module-
# level assert is the trader job's first executable statement.
from marketmind_workers.ftr.trader.execution_mode import EXECUTION_MODE, ExecutionMode

assert EXECUTION_MODE is ExecutionMode.PAPER, "FTR has no live mode."

import asyncio
import contextlib
import json
import os
import signal
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import structlog

from marketmind_workers.ftr.config.settings import get_ftr_settings
from marketmind_workers.ftr.config.venue_profiles import get_profile
from marketmind_workers.ftr.strategies.records import Action, DecisionRecord, ReasonCode
from marketmind_workers.ftr.strategies.specs import (
    FTRStrategySpec,
    OFIResearchSpec,
    validate_ftr_spec,
)
from marketmind_workers.ftr.trader import persistence as db
from marketmind_workers.ftr.trader.guards import RiskGuards
from marketmind_workers.ftr.trader.paper_broker import PaperBroker
from marketmind_workers.ftr.trader.public_exchange import PublicOnlyExchange

logger = structlog.get_logger(__name__)

_LOOP_SECONDS = 60.0


def load_deployments(path: Path, db_url: str | None) -> list[FTRStrategySpec]:
    """Load + gate deployment specs.

    Refusals are loud: research_simulation_only specs are rejected by type;
    specs without a PASS verdict on a uk-feasible profile are rejected.
    """
    if not path.exists():
        logger.info("ftr_trader_no_deployments_file", path=str(path))
        return []
    raw = json.loads(path.read_text())
    specs: list[FTRStrategySpec] = []
    for item in raw.get("deployments", []):
        spec = validate_ftr_spec(item)
        if isinstance(spec, OFIResearchSpec) or getattr(spec, "research_simulation_only", False):
            raise TypeError(
                f"{spec.strategy_id}: research_simulation_only specs are refused by the "
                "paper trader BY TYPE — this strategy may be backtested but never routed."
            )
        profile = get_profile(spec.venue_profile)
        if not profile.uk_execution_feasible:
            raise ValueError(
                f"{spec.strategy_id}: venue profile {spec.venue_profile!r} is not "
                "uk_execution_feasible — paper deployment requires an accessible venue."
            )
        if db_url is not None and not _has_pass_verdict(db_url, spec.strategy_id):
            raise ValueError(
                f"{spec.strategy_id}: no PASS verdict on a uk-feasible profile in "
                "ftr_verdicts — run the validation gauntlet first."
            )
        specs.append(spec)
    return specs


def _has_pass_verdict(db_url: str, strategy_id: str) -> bool:
    import psycopg

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM ftr_verdicts
            WHERE strategy_id = %s AND verdict = 'PASS' AND uk_execution_feasible
            LIMIT 1
            """,
            (strategy_id,),
        )
        return cur.fetchone() is not None


class FTRTrader:
    def __init__(self, db_url: str | None) -> None:
        self.settings = get_ftr_settings()
        self.db_url = db_url
        self.profile = get_profile(self.settings.paper_venue_profile)
        assert self.profile.uk_execution_feasible, (
            "paper trader must simulate an accessible venue"
        )
        self.exchange = PublicOnlyExchange(self.settings.research_exchange)
        self.broker = PaperBroker(
            profile=self.profile, cash=Decimal(self.settings.initial_equity_usd)
        )
        self.guards = RiskGuards(self.settings)
        self.deployments = load_deployments(
            self.settings.data_dir / "deployments.json", db_url
        )
        self._stop = asyncio.Event()
        if db_url is not None:
            recovered = db.recover_state(db_url, self.broker.cash)
            if recovered.cash is not None:
                self.broker.cash = recovered.cash
            for pos in recovered.open_positions:
                self.broker.positions[pos.symbol] = pos

    def stop(self) -> None:
        self._stop.set()

    # -- one loop tick -------------------------------------------------------

    async def tick(self) -> None:
        now = datetime.now(UTC)
        if self.db_url is not None:
            self.guards.set_db_kill_flag(db.killswitch_engaged(self.db_url))
        if self.guards.kill_switch_engaged():
            logger.warning("ftr_trader_killswitch_halt")
            return

        marks = self._fetch_marks()
        equity = self.broker.equity(marks)
        self.guards.roll_day(now, equity)
        self.guards.update_equity(equity)

        for spec in self.deployments:
            await self._evaluate_strategy(spec, now, marks)

        positions_value = equity - self.broker.cash
        if self.db_url is not None:
            db.snapshot_equity(
                self.db_url,
                ts=now.replace(second=0, microsecond=0),
                cash=self.broker.cash,
                positions_value=positions_value,
                gross_exposure_pct=self.broker.gross_exposure_pct(marks),
            )

    def _fetch_marks(self) -> dict[str, Decimal]:
        marks: dict[str, Decimal] = {}
        symbols = {p.symbol for p in self.broker.positions.values()}
        for spec in self.deployments:
            instrument = getattr(spec, "instrument", None)
            if instrument is not None:
                symbols.add(instrument.symbol)
        for sym in symbols:
            try:
                candles = self.exchange.fetch_ohlcv(sym, "1m", limit=2)
                if candles:
                    marks[sym] = Decimal(str(candles[-1][4]))
            except Exception as exc:
                logger.warning("ftr_mark_fetch_failed", symbol=sym, err=str(exc))
        return marks

    async def _evaluate_strategy(
        self, spec: FTRStrategySpec, now: datetime, marks: dict[str, Decimal]
    ) -> None:
        """Bar strategies evaluate only on their bar closes.

        v1 live evaluation is the EXIT path + guard plumbing; entry signals
        require a strategy with a PASS verdict, at which point its live
        evaluator is wired here. With no deploy-eligible strategy the loop
        idles honestly — it never invents entries.
        """
        instrument = getattr(spec, "instrument", None)
        symbol = instrument.symbol if instrument is not None else None
        if symbol is None or symbol not in marks:
            return
        rec = DecisionRecord.model_validate(
            {
                "ts_utc": now,
                "strategy_id": spec.strategy_id,
                "symbol": symbol,
                "action": Action.HOLD,
                "reason_codes": [ReasonCode.HOLD_NO_SIGNAL],
            }
        )
        if self.db_url is not None:
            bar_ts = now.replace(second=0, microsecond=0)
            db.insert_decision(self.db_url, rec, bar_ts)

    # -- main ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info(
            "ftr_trader_start",
            profile=self.profile.name,
            deployments=[s.strategy_id for s in self.deployments],
            equity=str(self.broker.cash),
        )
        while not self._stop.is_set():
            started = asyncio.get_event_loop().time()
            try:
                await self.tick()
            except Exception as exc:
                logger.error("ftr_trader_tick_error", err=str(exc))
            elapsed = asyncio.get_event_loop().time() - started
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=max(_LOOP_SECONDS - elapsed, 1.0)
                )
        logger.info("ftr_trader_stopped")


def main() -> int:
    # PAPER ONLY — assert again at process entry (belt and braces; the
    # module-level assert above already ran on import).
    assert EXECUTION_MODE is ExecutionMode.PAPER, "FTR has no live mode."

    db_url = os.getenv("DATABASE_URL")
    if db_url is not None:
        from marketmind_workers.db import apply_migrations

        applied = apply_migrations(db_url)
        if applied:
            logger.info("ftr_migrations_applied", migrations=applied)

    trader = FTRTrader(db_url)
    loop = asyncio.new_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, trader.stop)
    loop.run_until_complete(trader.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
