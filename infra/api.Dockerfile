# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the API. The uv base image is preloaded with
# uv + a Python toolchain; we use it for the dependency install stage
# and switch to a slim python image for the runtime to keep the final
# image small.
#
# Build from the repo root:
#   docker build -f infra/api.Dockerfile -t marketmind/api .

FROM ghcr.io/astral-sh/uv:0.5.4-python3.12-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Install dependencies first for layer caching: only the manifest files
# are copied at this stage, so unrelated source changes don't bust the
# wheel cache.
COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY api/pyproject.toml api/
COPY workers/pyproject.toml workers/

# Create the venv with all workspace deps. --no-install-workspace skips
# installing our own packages here — we add them after copying source.
# Cache mount removed for Railway compatibility (validator requires
# id=s/<service-id>-<path> format). Re-add with literal service IDs
# after worker + web services exist in Railway.
RUN uv sync --frozen --no-dev --no-install-workspace --package marketmind-api

# Now copy source and install the workspace packages as proper wheels
# (--no-editable). Without this, uv installs marketmind_{api,shared}
# editable, with .pth files pointing at /build/... in the builder stage
# — paths that don't exist in the runtime image, breaking imports.
# --no-editable builds wheels and installs them into /opt/venv, so the
# runtime needs only the venv, not the source tree.
COPY shared/ shared/
COPY api/ api/
# Cache mount removed for Railway compatibility (validator requires
# id=s/<service-id>-<path> format). Re-add with literal service IDs
# after worker + web services exist in Railway.
RUN uv sync --frozen --no-dev --no-editable --package marketmind-api


FROM python:3.12-slim-bookworm AS runtime

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for runtime — cheap defense-in-depth.
RUN groupadd --system app && useradd --system --gid app --create-home app
USER app

COPY --from=builder --chown=app:app /opt/venv /opt/venv

WORKDIR /app

EXPOSE 8000

# Shell-form CMD / HEALTHCHECK so ${PORT:-8000} expands at runtime.
# Railway assigns each service its own $PORT; binding to 8000
# unconditionally would make the platform's edge proxy unable to
# reach the container. Local docker compose leaves PORT unset so
# the fallback (8000) keeps that path working unchanged.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:${PORT:-8000}/health || exit 1

CMD uvicorn marketmind_api.main:app --host 0.0.0.0 --port ${PORT:-8000}
