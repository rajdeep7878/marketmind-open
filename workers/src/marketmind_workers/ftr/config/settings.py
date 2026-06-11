"""FTR settings — pydantic-settings, env-var driven, repo convention.

There is deliberately NO execution-mode setting here. Execution mode is the
single-member ``ExecutionMode.PAPER`` enum in ``ftr.trader.execution_mode``;
no environment variable or config key can introduce another mode
(test: test_ftr_no_live_env_escape).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Trend-universe superset (mandate Stage 1). Each must be listed as a spot
# pair on at least one uk_execution_feasible venue — verified at fetch time;
# any that is not gets dropped and logged in the fetch manifest.
UNIVERSE_SUPERSET: tuple[str, ...] = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "LINK/USDT",
    "LTC/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "DOT/USDT",
    "BCH/USDT",
    "UNI/USDT",
)


class FTRSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="FTR_",
    )

    # Paths — all under data/ which is gitignored.
    data_dir: Path = Field(default=Path("data/ftr"))

    # Research exchange for the primary series; reference economics only.
    research_exchange: str = Field(default="binance")
    primary_symbol: str = Field(default="BTC/USDT")
    cross_venue_exchange: str = Field(default="kraken")
    cross_venue_symbol: str = Field(default="BTC/USD")

    # 1m history depth (days) for paper-fill modeling + overlay spread est.
    minute_history_days: int = Field(default=180, ge=30, le=400)

    # Recorder (opt-in Docker profile).
    recorder_symbols: str = Field(default="BTC/USDT")
    recorder_record_eth: bool = Field(default=False)

    # Paper trader risk guards (fractions of equity unless stated).
    max_position_pct: float = Field(default=0.20, gt=0, le=0.25)
    max_gross_exposure_pct: float = Field(default=1.00, gt=0, le=1.0)
    daily_loss_stop_pct: float = Field(default=0.02, gt=0, le=0.05)
    max_drawdown_stop_pct: float = Field(default=0.10, gt=0, le=0.25)
    max_trades_per_day_global: int = Field(default=8, ge=1, le=50)
    per_symbol_cooldown_hours: int = Field(default=4, ge=0)
    initial_equity_usd: str = Field(default="10000")

    # Active venue profile for paper-trader cost simulation.
    paper_venue_profile: str = Field(default="kraken_pro_uk_tier0")

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"


@lru_cache(maxsize=1)
def get_ftr_settings() -> FTRSettings:
    return FTRSettings()
