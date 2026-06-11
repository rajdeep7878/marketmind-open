-- Idempotent init script run once when the postgres data dir is empty.
-- Reserved for things that need a superuser; the schema migrations
-- themselves live in infra/db/migrations/ and are also applied to the
-- init dir via docker-compose (and at worker startup as a safety net).

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "citext";    -- case-insensitive text (useful for tags/emails later)
