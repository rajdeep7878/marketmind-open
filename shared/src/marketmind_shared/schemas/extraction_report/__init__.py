"""Extraction-side schemas: the LLM's verdict on whether a strategy
in a piece of source content is precise enough to backtest, plus the
rules-and-claims report that always accompanies an extraction.

These are the data shapes the Phase 2.2 extraction service produces.
StrategySpec itself lives in strategy_spec/; here we add:

  - ExtractionVerdict: the four-way decision (fully / partially /
    not_extractable / not_a_strategy)
  - ExtractedRule: one rule the model identified in the source, with a
    quote and a confidence score
  - AuthorClaim: a performance assertion the source made (return,
    drawdown, win rate, ...) so we can later compare against our own
    backtest
  - ExtractionReport: the top-level report
  - ExtractionResult: the (spec, report) pair returned by extract_strategy

The strict-frozen-extra-forbid model conventions match strategy_spec/.
"""

from marketmind_shared.schemas.extraction_report.report import (
    ExtractionReport,
    ExtractionResult,
)
from marketmind_shared.schemas.extraction_report.rules import (
    AuthorClaim,
    AuthorClaimType,
    ExtractedRule,
)
from marketmind_shared.schemas.extraction_report.verdict import ExtractionVerdict

__all__ = [
    "AuthorClaim",
    "AuthorClaimType",
    "ExtractedRule",
    "ExtractionReport",
    "ExtractionResult",
    "ExtractionVerdict",
]
