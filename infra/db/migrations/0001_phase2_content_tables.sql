-- Phase 2.1: content ingestion + transcription + extraction tables.
--
-- The strategy spec lives in `extracted_strategies.spec_json` as
-- canonical Pydantic-serialized JSON. We deliberately do NOT split the
-- spec into relational columns: the schema evolves rapidly during
-- early phases and JSONB queries are good enough for now. Phase 3 may
-- promote frequently queried fields (instrument, primary_timeframe) to
-- generated columns once the access pattern is stable.

CREATE TABLE IF NOT EXISTS ingested_content (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL CHECK (source_type IN ('youtube', 'article', 'raw_text')),
    source_url TEXT,  -- NULL for raw_text
    content_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ingested_content_source_url_idx
    ON ingested_content (source_url)
    WHERE source_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS ingested_content_created_at_idx
    ON ingested_content (created_at DESC);


CREATE TABLE IF NOT EXISTS transcripts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content_id UUID NOT NULL REFERENCES ingested_content (id) ON DELETE CASCADE,
    language TEXT NOT NULL,
    full_text TEXT NOT NULL,
    segments_json JSONB NOT NULL,
    duration_seconds DOUBLE PRECISION NOT NULL,
    model_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS transcripts_content_id_idx
    ON transcripts (content_id);


CREATE TABLE IF NOT EXISTS extracted_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transcript_id UUID NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    spec_json JSONB NOT NULL,
    warnings_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS extracted_strategies_transcript_id_idx
    ON extracted_strategies (transcript_id);
