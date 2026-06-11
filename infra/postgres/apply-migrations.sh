#!/bin/sh
# Apply infra/db/migrations/*.sql in lexicographic order, on a fresh
# postgres data dir. Runs as part of docker-entrypoint-initdb.d after
# 00-init.sql has created the extensions.
#
# Outside of compose-up, the worker process applies the same files at
# startup (see workers/migrations.py). Both code paths use idempotent
# DDL (CREATE TABLE IF NOT EXISTS, etc.) so applying twice is safe.

set -euo pipefail

for f in /docker-entrypoint-initdb.d/migrations/*.sql; do
    echo "Applying migration: $f"
    psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --file "$f"
done
