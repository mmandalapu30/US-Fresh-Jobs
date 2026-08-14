#!/usr/bin/env bash
#
# Bring up the platform on a small always-free instance, pulling prebuilt images.
#
#   scp infra/deploy/bootstrap-pull.sh user@<IP>:
#   ssh user@<IP> 'sudo SITE=jobs.duckdns.org bash bootstrap-pull.sh'
#
# For hosts too small to build on. The web image alone needs more memory to build than a
# 1 GB instance has, so .github/workflows/images.yml builds in CI and this only downloads.
#
# SITE  the hostname Caddy should serve and obtain a certificate for. Leave it unset to
#       serve plain HTTP on the IP, which is the right first step when DNS is not
#       pointing here yet -- a certificate request that fails leaves Caddy retrying and
#       the site down, which looks like a broken deploy rather than a DNS problem.
set -uo pipefail

BRANCH="${BRANCH:-deploy/minio-and-worker-entrypoints}"
REPO="${REPO:-https://github.com/mmandalapu30/US-Fresh-Jobs.git}"
DIR="${DIR:-/srv/job-platform}"
SITE="${SITE:-}"
INGEST_SINCE="${INGEST_SINCE:-}"
SWAP_GB="${SWAP_GB:-3}"

log()  { echo; echo "=== $* ==="; }
die()  { echo "FAILED: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"

log "1/9  host"
MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
echo "  $(nproc) vCPU / ${MEM_MB} MB RAM / $(uname -m) / $(. /etc/os-release && echo "$PRETTY_NAME")"

# ---------------------------------------------------------------------------------------
# Swap is not optional here. Nothing builds on this host, but the ingest reads Parquet row
# groups and briefly needs more than the box has. Swap turns that into slow rather than
# killed.
log "2/9  swap"
if [ "$(swapon --show --noheadings | wc -l)" -gt 0 ]; then
  swapon --show
else
  fallocate -l "${SWAP_GB}G" /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB*1024)) status=none
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Reach for swap only under real pressure -- paging Postgres out during normal serving
  # would be worse than the ingest being slow.
  sysctl -qw vm.swappiness=10
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
  echo "  ${SWAP_GB}G swapfile active"
fi

log "3/9  firewall"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 22/tcp >/dev/null 2>&1
  ufw allow 80/tcp >/dev/null 2>&1
  ufw allow 443/tcp >/dev/null 2>&1
  ufw --force enable >/dev/null 2>&1
  echo "  22, 80, 443 open"
fi

log "4/9  docker"
if command -v docker >/dev/null 2>&1; then
  echo "  $(docker --version)"
else
  curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || die "docker install failed"
  echo "  $(docker --version)"
fi
systemctl enable --now docker >/dev/null 2>&1

log "5/9  source"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH" && git -C "$DIR" checkout -q "$BRANCH" && git -C "$DIR" pull -q
else
  mkdir -p "$(dirname "$DIR")"
  git clone -q -b "$BRANCH" "$REPO" "$DIR" || die "clone failed"
fi
cd "$DIR" || die "cannot enter $DIR"
echo "  $(git log --oneline -1)"

# ---------------------------------------------------------------------------------------
# Pin the tag to this exact commit rather than following `latest`. `latest` is only
# published from the default branch, so on any other branch it either does not exist or
# points at something else entirely -- and pinning means the running containers and the
# checked-out source always describe the same build.
SHA="$(git rev-parse HEAD)"
BRANCH_TAG="$(git rev-parse --abbrev-ref HEAD | tr '/' '-')"
export IMAGE_TAG="${IMAGE_TAG:-sha-$SHA}"
echo "  image tag: $IMAGE_TAG"

log "6/9  configuration"
if [ -f .env.production ]; then
  echo "  .env.production exists -- keeping it"
else
  PGPASS=$(openssl rand -hex 24)
  cp .env.production.example .env.production
  # Every secret is >=32 chars: the production hardening check requires it of all four,
  # including the object-storage access key. Hex, so nothing needs escaping in a URL.
  sed -i \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PGPASS}|" \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://jobplatform:${PGPASS}@postgres:5432/jobplatform|" \
    -e "s|^OBJECT_STORAGE_ACCESS_KEY=.*|OBJECT_STORAGE_ACCESS_KEY=$(openssl rand -hex 24)|" \
    -e "s|^OBJECT_STORAGE_SECRET_KEY=.*|OBJECT_STORAGE_SECRET_KEY=$(openssl rand -hex 24)|" \
    -e "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 48)|" \
    -e "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 48)|" \
    -e "s|^INGEST_CATEGORY_ALLOWLIST=.*|INGEST_CATEGORY_ALLOWLIST=data-engineering,software|" \
    .env.production
  chmod 600 .env.production
  grep -qE '^[A-Z_]+=.*(CHANGE_ME|REPLACE-WITH)' .env.production \
    && die "placeholders survived generation" || echo "  secrets generated"
fi

# SITE_ADDRESS and CORS have to agree, and CORS must be https or the API refuses to boot.
if [ -n "$SITE" ]; then
  sed -i -e "s|^SITE_ADDRESS=.*|SITE_ADDRESS=${SITE}|" \
         -e "s|^CORS_ALLOW_ORIGINS=.*|CORS_ALLOW_ORIGINS=https://${SITE}|" .env.production
  echo "  serving ${SITE} with automatic TLS"
else
  sed -i -e "s|^SITE_ADDRESS=.*|SITE_ADDRESS=:80|" \
         -e "s|^CORS_ALLOW_ORIGINS=.*|CORS_ALLOW_ORIGINS=https://localhost|" .env.production
  echo "  serving plain HTTP on :80 (no SITE given)"
fi

C="docker compose -f infra/docker/docker-compose.prod.yml -f infra/docker/docker-compose.small.yml --env-file .env.production"

log "7/9  pulling images"
# MinIO is deliberately excluded: nothing writes to the archive, and its container is the
# single largest saving available on a 1 GB host.
$C pull postgres redis api web caddy 2>&1 | tail -6 || die "pull failed -- are the images published for $IMAGE_TAG?"
$C up -d postgres redis api web caddy || die "stack failed to start"
sleep 25
docker ps --format 'table {{.Names}}\t{{.Status}}'

log "8/9  database schema"
$C --profile ingest run --rm --no-deps ingest \
  python -m alembic -c database/migrations/alembic.ini upgrade head || die "migration failed"

log "9/9  first ingest"
if [ -n "$INGEST_SINCE" ]; then
  echo "  bounded to files on or after $INGEST_SINCE"
  $C --profile ingest run --rm ingest python scripts/ingest.py --trigger MANUAL --since "$INGEST_SINCE"
else
  echo "  full backfill -- slow on a shared-core host, expect a couple of hours"
  COMPOSE=1 ./scripts/daily.sh
fi

log "schedule"
./scripts/install-systemd.sh && systemctl list-timers 'jobplatform-*' --no-pager

log "DONE"
if [ -n "$SITE" ]; then echo "  https://${SITE}/"; else echo "  http://$(curl -s --max-time 10 ifconfig.me || echo '<ip>')/"; fi
