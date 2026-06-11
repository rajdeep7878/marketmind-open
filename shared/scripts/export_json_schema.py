"""Export Pydantic models as JSON Schema for downstream TS-type generation.

Run:
    uv run python shared/scripts/export_json_schema.py

Output goes to `web/src/types/generated/schemas.json`.

This script's output is committed. CI verifies it is up to date —
see the diff-check step in .github/workflows/ci.yml. The frontend's
build step then runs `json-schema-to-typescript` against the committed
JSON to produce strict TS types (those are NOT committed; regenerate
locally). One-way Python -> JSON Schema -> TS keeps Pydantic as the
single source of truth.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from marketmind_shared import SCHEMA_VERSION
from marketmind_shared.schemas import (
    ArticleContent,
    AuthorClaim,
    AuthorClaimComparison,
    BacktestMeta,
    BacktestMetrics,
    BacktestResult,
    BacktestRun,
    BenchmarkComparison,
    BenchmarkEquityPoint,
    BenchmarkResult,
    DeflatedSharpeResult,
    EquityPoint,
    ExtractedRule,
    ExtractionInput,
    ExtractionReport,
    ExtractionResult,
    IngestedContent,
    JobSubmission,
    JobView,
    MonteCarloResult,
    OverfittingAnalysis,
    OverfittingScore,
    ParameterSweepResult,
    RawTextContent,
    StrategySpec,
    Trade,
    Transcript,
    TranscriptSegment,
    WalkForwardResult,
    YouTubeContent,
)
from pydantic import BaseModel, TypeAdapter

# Each entry becomes a top-level definition in the combined schema bundle.
# Add new models here as the schema grows.
EXPORTED_MODELS: dict[str, Any] = {
    "JobSubmission": JobSubmission,
    "JobView": JobView,
    "StrategySpec": StrategySpec,
    "YouTubeContent": YouTubeContent,
    "ArticleContent": ArticleContent,
    "RawTextContent": RawTextContent,
    "TranscriptSegment": TranscriptSegment,
    "Transcript": Transcript,
    "ExtractionInput": ExtractionInput,
    "ExtractedRule": ExtractedRule,
    "AuthorClaim": AuthorClaim,
    "ExtractionReport": ExtractionReport,
    "ExtractionResult": ExtractionResult,
    "EquityPoint": EquityPoint,
    "Trade": Trade,
    "BacktestMeta": BacktestMeta,
    "BacktestRun": BacktestRun,
    "BacktestMetrics": BacktestMetrics,
    "BenchmarkEquityPoint": BenchmarkEquityPoint,
    "BenchmarkResult": BenchmarkResult,
    "BenchmarkComparison": BenchmarkComparison,
    "AuthorClaimComparison": AuthorClaimComparison,
    "BacktestResult": BacktestResult,
    "WalkForwardResult": WalkForwardResult,
    "ParameterSweepResult": ParameterSweepResult,
    "MonteCarloResult": MonteCarloResult,
    "DeflatedSharpeResult": DeflatedSharpeResult,
    "OverfittingScore": OverfittingScore,
    "OverfittingAnalysis": OverfittingAnalysis,
}

# Discriminated unions aren't BaseModels, so they need a TypeAdapter to
# emit a json_schema. Keep this out of EXPORTED_MODELS so the simple
# "BaseModel with .model_json_schema" path stays the common case.
EXPORTED_UNIONS: dict[str, TypeAdapter[Any]] = {
    "IngestedContent": TypeAdapter(IngestedContent),
}


def build_bundle() -> dict[str, object]:
    definitions: dict[str, object] = {
        name: model.model_json_schema(mode="serialization")
        for name, model in EXPORTED_MODELS.items()
    }
    for name, adapter in EXPORTED_UNIONS.items():
        definitions[name] = adapter.json_schema(mode="serialization")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://marketmind.ai/schemas/{SCHEMA_VERSION}",
        "title": "MarketMind AI shared schemas",
        "schemaVersion": SCHEMA_VERSION,
        "definitions": definitions,
    }


def _first_paragraph(doc: str) -> str:
    """Collapse the first paragraph of a docstring into one clean line."""
    first = doc.strip().split("\n\n", 1)[0]
    return " ".join(first.split())


def _docstring_registry() -> dict[str, str]:
    """Map StrategySpec-package model name -> its one-line docstring summary.

    Pydantic emits a class docstring as the JSON-Schema `description`
    *inconsistently*: recursive models rebuilt via `model_rebuild()`
    (e.g. RatchetExpr, RegimeStateCondition) silently lose it, while
    leaf models keep it. `_inject_model_descriptions` uses this registry
    to restore the missing ones deterministically so the schema the LLM
    sees teaches every condition/expression type, not just the leaves.
    """
    from marketmind_shared.schemas import strategy_spec as ss_pkg

    registry: dict[str, str] = {}
    for name in ss_pkg.__all__:
        obj = getattr(ss_pkg, name, None)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.__doc__:
            registry[name] = _first_paragraph(obj.__doc__)
    return registry


def _inject_model_descriptions(node: object, registry: dict[str, str]) -> None:
    """Walk a JSON-Schema tree; for any model object-schema whose `title`
    names a registered model and which carries no `description`, inject
    the model's docstring summary.

    Purely additive — schemas that already have a description (Pydantic
    got it right) are left untouched, so the bundle stays minimal and
    the diff is limited to the models Pydantic actually dropped.
    """
    if isinstance(node, dict):
        title = node.get("title")
        if (
            isinstance(title, str)
            and title in registry
            and not node.get("description")
            and ("properties" in node or node.get("type") == "object")
        ):
            node["description"] = registry[title]
        for value in node.values():
            _inject_model_descriptions(value, registry)
    elif isinstance(node, list):
        for item in node:
            _inject_model_descriptions(item, registry)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    out_path = repo_root / "web" / "src" / "types" / "generated" / "schemas.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle()
    _inject_model_descriptions(bundle, _docstring_registry())
    out_path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    definitions = bundle["definitions"]
    assert isinstance(definitions, dict)
    print(f"Wrote {out_path.relative_to(repo_root)} ({len(definitions)} models)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
