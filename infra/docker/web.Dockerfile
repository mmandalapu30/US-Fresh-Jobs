# Multi-stage: install once, build once, ship Next's standalone server as a non-root user.
#
# Build context is the repo root, matching the other images:
#     docker build -f infra/docker/web.Dockerfile .
FROM node:22-alpine AS deps

WORKDIR /build
# Only the manifests, so a source-only change does not invalidate the install layer.
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci


FROM node:22-alpine AS builder

WORKDIR /build
COPY --from=deps /build/node_modules ./node_modules
COPY apps/web ./

# No API_BASE_URL is passed here, and that is deliberate. Every page that reads it is
# server-rendered on demand (`export const dynamic = "force-dynamic"`), and next.config.mjs
# no longer declares it under `env`, which would inline it into the bundle at build time.
# So this build never contacts the API -- it cannot, there is no api service during a
# docker build -- and one image runs in any environment.
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build


FROM node:22-alpine AS runtime

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3100 \
    HOSTNAME=0.0.0.0

# wget is in busybox already, so the HEALTHCHECK below needs no extra package.
RUN addgroup --system --gid 10002 nodejs \
 && adduser --system --uid 10002 --ingroup nodejs nextjs

WORKDIR /app

# `output: "standalone"` emits a self-contained server plus only the node_modules it
# actually reaches, which is why the runtime stage copies no package manifests at all.
COPY --from=builder --chown=nextjs:nodejs /build/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /build/.next/static ./.next/static

USER nextjs
EXPOSE 3100

# 127.0.0.1, not localhost. Next binds IPv4 only (HOSTNAME=0.0.0.0) while `localhost`
# also resolves to ::1 -- and busybox wget tries ::1 and gives up, rather than falling
# back to IPv4 the way curl does. With `localhost` this check failed in ~20ms every run,
# leaving a container that served traffic perfectly while reporting unhealthy forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -qO- http://127.0.0.1:3100/ >/dev/null 2>&1 || exit 1

CMD ["node", "server.js"]
