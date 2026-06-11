"""Trader v1 worker configuration.

Adds the `TRADER_*` env-var surface from the implementation prompt as
a sibling to `WorkerSettings`. Same pydantic-settings + lru-cache +
env_file=".env" pattern as the rest of the codebase.

`assert_paper_only()` is the single hard guard against live execution
in v1. It must be the literal first call in every trader job callable
(see `workers.trader.jobs`), the runner entrypoint, and any admin route
that toggles a strategy's paper-approval flag. The env var stays unset
in v1; anything other than 'false' (case-insensitive) crashes the
process.
"""

from __future__ import annotations

import os
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class TraderSettings(BaseSettings):
    """All TRADER_* env vars. Loaded from process env or .env at the
    repo root, same as WorkerSettings.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "staging", "production", "test"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Same defaults as WorkerSettings so a host-side `uv run` lands on
    # the local compose Postgres / Redis without further env wiring.
    database_url: PostgresDsn = Field(
        default="postgresql://marketmind:marketmind_dev@localhost:5432/marketmind",  # type: ignore[arg-type]
    )
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379/0",  # type: ignore[arg-type]
    )

    # Dedicated RQ queue. The trader_worker process listens here; the
    # existing `worker` process listens on `default`. Separation
    # prevents a long-running backtest job on the default queue from
    # head-of-line blocking a signal-execution cycle.
    trader_queue_name: str = "trader_default"

    # Default symbols + timeframes the ingestion loop pulls. The signal
    # loop reads each version's own symbols/timeframes; the ingestion
    # loop covers the union of (config defaults) and (every enabled
    # version's needs) so a strategy on a non-default symbol still has
    # candles when its first signal fires.
    trader_symbols: str = "BTC/USDT,ETH/USDT"
    # Phase B.3 (2026-05-23): added "1h" alongside the v1 "4h" — the
    # ingestion + signal loops are already TF-agnostic (see B.3 design
    # finding in docs/design/v2-phase-b-lower-timeframes.md §3). The 4H
    # strategies seeded in Phase A continue to evaluate only on their
    # own declared timeframe (intersection-gated in signal_engine); the
    # 1H rows in trader_candles accumulate as foundation for B.7+.
    # Phase B.8 (2026-05-23): added "15m" alongside 4h+1h — same
    # pattern, 4× higher cadence than 1H, ~96 bars/day per symbol.
    trader_timeframes: str = "4h,1h,15m"

    # Starting cash. GBP because v1 ledgers in GBP for operator-facing
    # reporting; the trader still trades USDT-quoted pairs and the
    # conversion is a static reporting transform.
    trader_starting_cash_gbp: Decimal = Decimal("1000")

    # Risk caps. Decimal proportions: 0.01 == 1%.
    trader_max_risk_per_trade_pct: Decimal = Decimal("0.01")
    trader_max_portfolio_risk_pct: Decimal = Decimal("0.05")
    trader_max_daily_loss_pct: Decimal = Decimal("0.02")
    trader_max_weekly_loss_pct: Decimal = Decimal("0.05")
    trader_max_drawdown_pct: Decimal = Decimal("0.10")

    # Stale-data threshold. 4h candles get a 10-minute grace; override
    # via env when running on smaller timeframes.
    trader_data_staleness_seconds: int = 600

    # Default fee / slippage applied when a strategy version doesn't
    # set its own. 10 bps = 0.10% — matches the backtest engine's
    # DEFAULT_COST_MODEL so paper results align with the approving
    # backtest by default.
    trader_default_fee_bps: Decimal = Decimal("10")
    trader_default_slippage_bps: Decimal = Decimal("10")

    # Live-trading guard. v1 pins this to "false"; any other value
    # makes `assert_paper_only()` crash the process at job entry.
    # Kept as a string field (not bool) so the assertion can compare
    # the raw env value rather than the parsed bool — easier to
    # diagnose at the system-boundary.
    trader_allow_live: str = "false"

    # Telegram delivery. Both optional; if either is empty the alert
    # dispatcher writes the DB row but skips the network call.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Documented for the env-var inventory. The trader code never
    # reads ANTHROPIC_API_KEY — paper-only invariant: no LLM in the
    # decision path.
    anthropic_api_key: str = ""

    def symbols_list(self) -> list[str]:
        """Parse TRADER_SYMBOLS into a list; drop empties + trim ws."""
        return [s.strip() for s in self.trader_symbols.split(",") if s.strip()]

    def timeframes_list(self) -> list[str]:
        """Parse TRADER_TIMEFRAMES into a list; drop empties + trim ws."""
        return [t.strip() for t in self.trader_timeframes.split(",") if t.strip()]

    def assert_symbols_homogeneous_asset_class(self) -> None:
        """Phase C C.1.5: enforce the homogeneous-class invariant.

        The C.1.4 ingestion loop dispatches a SINGLE adapter per cycle,
        selected from the first symbol's inferred asset_class. Mixed
        TRADER_SYMBOLS deployments (e.g. "BTC/USDT,EUR/USD") would
        silently use the BTC-inferred BinanceAdapter to fetch EUR/USD
        candles, producing zero rows or an opaque error. Raise at boot
        instead, with a message that names the offending symbols and
        their inferred classes + points to the sub-phase where multi-
        class loops land.

        Empty TRADER_SYMBOLS is silently accepted — the ingestion loop
        has its own fallback for the no-symbols case (see
        ingestion.py:472).

        This is called explicitly from runner.main()'s startup sequence
        (NOT from the field validator) so a misconfigured TRADER_SYMBOLS
        crashes the runner with a clear traceback rather than a
        deep-stack error inside a job callable.
        """
        # Local import — module-level would create a circular dependency
        # (exchanges → schemas → settings).
        from marketmind_workers.trader.exchanges import infer_asset_class_from_symbol

        symbols = self.symbols_list()
        if not symbols:
            return
        per_symbol: list[tuple[str, str]] = []
        for sym in symbols:
            try:
                per_symbol.append((sym, infer_asset_class_from_symbol(sym)))
            except ValueError as exc:
                # Inference itself failed — surface that as part of the
                # homogeneity error so an operator sees one consolidated
                # message at boot.
                raise ValueError(
                    f"TRADER_SYMBOLS contains {sym!r} which cannot be "
                    f"classified into a known asset class: {exc}",
                ) from exc
        classes = {ac for _, ac in per_symbol}
        if len(classes) > 1:
            breakdown = ", ".join(f"{sym} → {ac}" for sym, ac in per_symbol)
            raise ValueError(
                f"TRADER_SYMBOLS={self.trader_symbols!r} mixes asset "
                f"classes: {sorted(classes)}. Breakdown: {breakdown}. "
                "The Phase C C.1.4 ingestion loop uses ONE adapter per "
                "cycle (dispatched on the first symbol's inferred class). "
                "Mixed-asset-class deployments are not supported until "
                "Phase C C.5 / C.6 / C.7 ship the per-pair adapter map. "
                "Until then, TRADER_SYMBOLS must be homogeneous per "
                "asset class.",
            )


@lru_cache(maxsize=1)
def get_trader_settings() -> TraderSettings:
    """Lru-cached singleton accessor. Mirrors get_settings in
    api / workers.
    """
    return TraderSettings()  # type: ignore[call-arg]


def assert_paper_only() -> None:
    """Hard guard against live execution in v1.

    Reads `TRADER_ALLOW_LIVE` directly from `os.environ` rather than
    via TraderSettings so the assertion still fires before any
    pydantic-settings parse happens (e.g. when called from the very
    top of a job callable). AssertionError on mismatch — crashes the
    process loudly and produces a clear traceback. The implementation
    prompt specifies this guard verbatim.
    """
    value = os.getenv("TRADER_ALLOW_LIVE", "false").lower()
    assert value == "false", (
        f"Live trading is disabled in v1. TRADER_ALLOW_LIVE={value!r} — must be 'false'."
    )


__all__ = [
    "TraderSettings",
    "assert_paper_only",
    "get_trader_settings",
]
