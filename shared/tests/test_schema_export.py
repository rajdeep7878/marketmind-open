from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_export_module():
    # Load the standalone script as a module so we can call build_bundle()
    # without running its __main__ block.
    here = Path(__file__).resolve()
    script = here.parents[1] / "scripts" / "export_json_schema.py"
    spec = importlib.util.spec_from_file_location("export_json_schema", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_contains_expected_models() -> None:
    mod = _load_export_module()
    bundle = mod.build_bundle()
    assert bundle["$schema"].startswith("https://json-schema.org/")
    assert "definitions" in bundle
    expected = {
        "JobSubmission",
        "JobView",
        "StrategySpec",
        "YouTubeContent",
        "ArticleContent",
        "RawTextContent",
        "TranscriptSegment",
        "Transcript",
        "ExtractionInput",
        "IngestedContent",
        "ExtractedRule",
        "AuthorClaim",
        "ExtractionReport",
        "ExtractionResult",
        "EquityPoint",
        "Trade",
        "BacktestMeta",
        "BacktestRun",
    }
    assert expected <= set(bundle["definitions"].keys())


def test_bundle_is_json_serializable() -> None:
    mod = _load_export_module()
    bundle = mod.build_bundle()
    # If this throws, json-schema-to-typescript on the web side would also fail.
    json.dumps(bundle)
