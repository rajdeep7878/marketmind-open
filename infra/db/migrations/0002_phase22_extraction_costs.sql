-- Phase 2.2: track per-extraction token usage + dollar cost.
--
-- One row per LLM-extraction job (retries roll up into the single row
-- via combined input/output counts on the service side). Linked to
-- extracted_strategies by FK; ON DELETE CASCADE so removing a strategy
-- cleans up its cost trail.

CREATE TABLE IF NOT EXISTS extraction_costs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extracted_strategy_id UUID NOT NULL
        REFERENCES extracted_strategies (id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS extraction_costs_strategy_idx
    ON extraction_costs (extracted_strategy_id);

CREATE INDEX IF NOT EXISTS extraction_costs_created_at_idx
    ON extraction_costs (created_at DESC);
