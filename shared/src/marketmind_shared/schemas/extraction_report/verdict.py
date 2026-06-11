"""The four extraction verdicts.

Defined as a StrEnum so serialization is the lowercase string ("fully_extractable")
rather than the qualified member name. Matches the convention established in
strategy_spec/common.py (Timeframe, Direction, etc.).
"""

from __future__ import annotations

from enum import StrEnum


class ExtractionVerdict(StrEnum):
    """Outcome of an extraction attempt.

    - fully_extractable: all critical fields present and precise;
      StrategySpec is fully populated.
    - partially_extractable: spec is present but some fields are
      defaulted or missing; flagged in extraction_notes.
    - not_extractable: the source's entry/exit logic requires human
      judgment (manually drawn levels, subjective patterns, ICT/SMC,
      harmonics, etc.). spec is null.
    - not_a_strategy: the source contains no trading rules at all —
      it's market commentary, news, opinion, or pure promotion.
      spec is null.
    """

    FULLY_EXTRACTABLE = "fully_extractable"
    PARTIALLY_EXTRACTABLE = "partially_extractable"
    NOT_EXTRACTABLE = "not_extractable"
    NOT_A_STRATEGY = "not_a_strategy"


__all__ = ["ExtractionVerdict"]
