"""The top-level ExtractionReport plus the (spec, report) wrapper.

ExtractionReport is the always-present companion to a StrategySpec
extraction. Even when the verdict is `not_extractable` or
`not_a_strategy` (and `spec` is therefore null), the report is still
returned so the UI can show *why* we refused.

ExtractionResult is the (spec, report) pair the service returns. The
extracted_strategies table persists `spec_json` + a derived
`warnings_json`; the full ExtractionResult is reconstructed on read.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from marketmind_shared.schemas.extraction_report.rules import (
    AuthorClaim,
    ExtractedRule,
)
from marketmind_shared.schemas.extraction_report.verdict import ExtractionVerdict
from marketmind_shared.schemas.strategy_spec.common import _StrictModel
from marketmind_shared.schemas.strategy_spec.spec import StrategySpec


class ExtractionReport(_StrictModel):
    """The structured explanation that accompanies every extraction.

    Every field is required because the prompt is meant to produce a
    complete report regardless of verdict. `refusal_explanation` is the
    one nullable field — it's None for successful extractions and a
    string for refusals.
    """

    verdict: ExtractionVerdict
    overall_confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1, max_length=2000)
    extracted_rules: list[ExtractedRule] = Field(default_factory=list)
    backtestable_parts: list[str] = Field(default_factory=list)
    non_backtestable_parts: list[str] = Field(default_factory=list)
    author_claims: list[AuthorClaim] = Field(default_factory=list)
    reasoning: str = Field(min_length=1, max_length=4000)
    refusal_explanation: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _refusal_explanation_consistent(self) -> Self:
        """refusal_explanation must be present iff the verdict refuses.

        Catches model output drift where the LLM forgets to populate
        the explanation on a refusal, or accidentally fills it on a
        successful extraction.
        """
        refuses = self.verdict in (
            ExtractionVerdict.NOT_EXTRACTABLE,
            ExtractionVerdict.NOT_A_STRATEGY,
        )
        has_explanation = bool(self.refusal_explanation and self.refusal_explanation.strip())
        if refuses and not has_explanation:
            raise PydanticCustomError(
                "refusal_explanation_required",
                "refusal_explanation is required when verdict={verdict}",
                {"verdict": str(self.verdict)},
            )
        if not refuses and has_explanation:
            raise PydanticCustomError(
                "refusal_explanation_forbidden",
                "refusal_explanation must be null/empty when verdict={verdict}",
                {"verdict": str(self.verdict)},
            )
        return self


class ExtractionResult(_StrictModel):
    """The (spec, report) pair returned by extract_strategy.

    `spec` is None for non-extractable verdicts. The model_validator
    enforces this iff-relationship so the UI can dispatch on `verdict`
    alone without worrying about inconsistent state.
    """

    spec: StrategySpec | None = None
    report: ExtractionReport

    @model_validator(mode="after")
    def _spec_consistent_with_verdict(self) -> Self:
        """A spec must accompany an extractable verdict, and vice versa."""
        verdict = self.report.verdict
        extractable_verdicts = (
            ExtractionVerdict.FULLY_EXTRACTABLE,
            ExtractionVerdict.PARTIALLY_EXTRACTABLE,
        )
        if verdict in extractable_verdicts and self.spec is None:
            raise PydanticCustomError(
                "spec_required_for_extractable_verdict",
                "verdict={verdict} requires a non-null spec",
                {"verdict": str(verdict)},
            )
        if verdict not in extractable_verdicts and self.spec is not None:
            raise PydanticCustomError(
                "spec_forbidden_for_refusal_verdict",
                "verdict={verdict} requires spec to be null",
                {"verdict": str(verdict)},
            )
        return self


__all__ = ["ExtractionReport", "ExtractionResult"]
