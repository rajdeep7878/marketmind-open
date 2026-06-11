-- Phase 2.2 fix: spec_json must be NULLable to support refusal verdicts.
--
-- The Phase 2.1 schema declared spec_json NOT NULL because at the time
-- extracted_strategies was only ever populated by a successful extraction.
-- Phase 2.2 introduced the four-way verdict (fully / partially /
-- not_extractable / not_a_strategy) where the latter two carry spec=NULL.
-- ExtractionResult's model_validator enforces the spec<->verdict iff rule
-- in Python, and the repo always writes the report regardless of spec
-- state — so the DB column needs to match.

ALTER TABLE extracted_strategies
    ALTER COLUMN spec_json DROP NOT NULL;

COMMENT ON COLUMN extracted_strategies.spec_json IS
    'NULL when verdict is not_extractable or not_a_strategy. '
    'ExtractionResult.spec mirrors this.';
