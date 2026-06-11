# syntax=docker/dockerfile:1.7
#
# Next.js image. In dev we bind-mount the source and run `pnpm dev`;
# in prod we use the standalone output for a minimal runtime image.
#
# Build from the repo root:
#   docker build -f infra/web.Dockerfile -t marketmind/web .

FROM node:20.18-alpine AS base
ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
# node:20.18-alpine ships a corepack version that pre-dates npm's
# rotated registry signing keys, so `corepack enable` aborts with
# "Cannot find matching keyid" when fetching pnpm. Pulling the latest
# corepack first picks up the new keys.
RUN npm install -g corepack@latest && corepack enable

# ---- deps: install node_modules with pnpm fetch + install (deterministic)
FROM base AS deps
WORKDIR /app
COPY web/package.json web/pnpm-lock.yaml* ./
# Cache mount removed for Railway compatibility (validator requires
# id=s/<service-id>-<path> format). Re-add with literal service IDs
# after worker + web services exist in Railway.
RUN pnpm install --frozen-lockfile

# ---- builder: produce the standalone Next.js build
FROM base AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./

# NEXT_PUBLIC_* env vars are inlined into the client bundle AT BUILD
# TIME. Declaring them as ARG lets `docker build --build-arg` (and
# Railway, which passes service variables to declared ARGs
# automatically) push them through; promoting each ARG to ENV with
# the same name makes them visible to Next's "Collecting page data"
# pass, where web/src/lib/env.ts validates NEXT_PUBLIC_API_URL via
# zod and fails the build if it's missing.
#
# Local dev (docker compose target: dev) uses `next dev` instead and
# never runs `pnpm build`, which is why this only bites prod images.
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_PLAUSIBLE_DOMAIN
ARG NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_PLAUSIBLE_DOMAIN=$NEXT_PUBLIC_PLAUSIBLE_DOMAIN \
    NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL=$NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL \
    NEXT_OUTPUT=standalone \
    NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

# ---- dev: used by docker-compose for local dev with HMR
FROM base AS dev
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./
EXPOSE 3000
CMD ["pnpm", "dev"]

# ---- runtime: minimal prod image (Phase 6 deploy target)
FROM node:20.18-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
