"""Per-rule and per-claim records inside an ExtractionReport.

ExtractedRule: one item the LLM identified — could be backtestable
(e.g., "50-period SMA on close") or not (e.g., "I draw support by
eye"). The `extractable` boolean + the confidence score together let
the UI surface which parts are mechanical and which require human
judgment.

AuthorClaim: a performance assertion the source made. Pulled out as a
discrete record so Phase 3's backtester can compare its own results
against what the trader claimed.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from marketmind_shared.schemas.strategy_spec.common import _StrictModel


class AuthorClaimType(StrEnum):
    """Categories of performance claims a source might make.

    `other` is the escape hatch for anything that doesn't fit (e.g.,
    "I make $250k a week"). The enum is fixed so dashboards can
    aggregate cleanly.
    """

    RETURN = "return"
    DRAWDOWN = "drawdown"
    WIN_RATE = "win_rate"
    TRADE_COUNT = "trade_count"
    SHARPE = "sharpe"
    OTHER = "other"


class ExtractedRule(_StrictModel):
    """One rule the LLM identified in the source.

    `field`: free-text label for which spec field this rule covers
    (e.g., "entry.indicator_fast", "exit.stop_loss"). Not constrained
    to StrategySpec field names so non-backtestable rules
    ("re-entry_logic") can still be recorded.

    `value_description`: plain-English description of the rule.

    `extractable`: whether a computer could mechanically follow this
    rule given historical price data alone.

    `confidence`: 0.0 to 1.0. 0.95+ means the source stated this with a
    specific number; 0.5-0.75 means we inferred it; < 0.25 means we
    have no real basis.

    `quote`: optional source quote substantiating the rule.
    """

    field: str = Field(min_length=1, max_length=200)
    value_description: str = Field(min_length=1, max_length=2000)
    extractable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    quote: str | None = Field(default=None, max_length=2000)


class AuthorClaim(_StrictModel):
    """A performance claim made by the source.

    Kept separate from ExtractedRule because claims are NOT rules — they
    are assertions about a strategy's behaviour that we want to verify
    via our own backtest. Capturing them lets Phase 3 emit a "claimed X%,
    measured Y%" diff per backtested strategy.
    """

    claim_type: AuthorClaimType
    value: str = Field(min_length=1, max_length=200)
    timeframe: str | None = Field(default=None, max_length=50)
    instrument: str | None = Field(default=None, max_length=100)
    period: str | None = Field(default=None, max_length=100)
    quote: str = Field(min_length=1, max_length=2000)


__all__ = ["AuthorClaim", "AuthorClaimType", "ExtractedRule"]
