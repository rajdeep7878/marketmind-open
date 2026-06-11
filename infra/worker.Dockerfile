# syntax=docker/dockerfile:1.7
#
# Same pattern as api.Dockerfile but for the workers service. Kept as a
# separate file (rather than a single multi-target image) so each service
# has an obvious, independently-buildable entry point.

FROM ghcr.io/astral-sh/uv:0.5.4-python3.12-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY api/pyproject.toml api/
COPY workers/pyproject.toml workers/

# Cache mount removed for Railway compatibility (validator requires
# id=s/<service-id>-<path> format). Re-add with literal service IDs
# after worker + web services exist in Railway.
RUN uv sync --frozen --no-dev --no-install-workspace --package marketmind-workers

# Install workspace packages as wheels (not editable). Same reason as
# api.Dockerfile: editable installs point .pth at /build/... paths that
# don't survive into the runtime stage. See api.Dockerfile for the
# fuller comment.
COPY shared/ shared/
COPY workers/ workers/
# workers/pyproject.toml's hatch.force-include pulls these into the
# wheel so the worker can find them via importlib.resources after a
# --no-editable install. Need the sources on disk at build time for
# the force-include resolver to find them.
#   - infra/db/migrations/*.sql  -> marketmind_workers/_migrations/
#   - web/.../schemas.json       -> marketmind_workers/_schemas.json
COPY infra/db/migrations/ infra/db/migrations/
COPY web/src/types/generated/schemas.json web/src/types/generated/schemas.json
# Cache mount removed for Railway compatibility (validator requires
# id=s/<service-id>-<path> format). Re-add with literal service IDs
# after worker + web services exist in Railway.
RUN uv sync --frozen --no-dev --no-editable --package marketmind-workers


FROM python:3.12-slim-bookworm AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg + ffprobe are runtime deps:
#   - yt-dlp uses ffmpeg to remux/convert downloaded audio
#   - faster-whisper decodes audio frames via av/torchaudio, which call ffmpeg
#   - our transcription pre-flight calls ffprobe directly to read duration
# Installed here (runtime stage), not in the builder, so the final image
# carries the binaries. The worker's startup check refuses to boot
# without them, so this layer is load-bearing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --create-home app
USER app

COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /app

CMD ["python", "-m", "marketmind_workers.worker"]
