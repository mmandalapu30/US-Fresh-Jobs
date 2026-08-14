#!/usr/bin/env bash
#
# Bring a fresh Oracle ARM instance all the way to a serving deployment.
#
#   scp infra/oracle/bootstrap-server.sh ubuntu@<IP>:
#   ssh ubuntu@<IP> 'sudo bash bootstrap-server.sh'
#
# Idempotent: safe to re-run after fixing something. Every step announces itself, and the
# script stops at the first genuine failure rather than carrying on with a broken stack.
set -uo pipefail

BRANCH="${BRANCH:-deploy/minio-and-worker-entrypoints}"
REPO="${REPO:-https://github.com/mmandalapu30/US-Fresh-Jobs.git}"
DIR="${DIR:-/srv/job-platform}"
INGEST_SINCE="${INGEST_SINCE:-}"      # e.g. 2026-08-01 to bound the first load

log()  { echo; echo "=== $* ==="; }
die()  { echo "FAILED: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"

# ---------------------------------------------------------------------------------------
log "1/9  host"
MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
CPUS=$(nproc)
echo "  ${CPUS} vCPU / ${MEM_MB} MB RAM / $(uname -m)"

# ---------------------------------------------------------------------------------------
# Swap, sized to cover the build peak. `npm run build` is the memory high-water mark of
# the whole deploy and it is what OOMs a small instance -- but it is also a one-off, so
# swap is the right tool: slow for a few minutes beats not deploying at all.
log "2/9  swap"
if [ "$(swapon --show --noheadings | wc -l)" -gt 0 ]; then
  echo "  swap already present:"; swapon --show
elif [ "$MEM_MB" -ge 15000 ]; then
  echo "  ${MEM_MB} MB RAM -- ample, skipping swap"
else
  SWAP_GB=8
  echo "  ${MEM_MB} MB RAM -- adding ${SWAP_GB}G swapfile"
  fallocate -l ${SWAP_GB}G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB*1024))
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Only reach for swap under real pressure; swapping Postgres pages early would be worse
  # than the build being slow.
  sysctl -qw vm.swappiness=10
  grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
  swapon --show
fi

# ---------------------------------------------------------------------------------------
# Oracle's Ubuntu images ship a REJECT rule that drops 80/443 inside the OS, regardless of
# the VCN security list. Opening the console rules alone leaves the site unreachable, and
# the symptom -- a connection that hangs then dies -- looks exactly like broken DNS.
log "3/9  host firewall"
for port in 80 443; do
  if iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    echo "  :$port already allowed"
  else
    iptables -I INPUT 1 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
    echo "  :$port opened"
  fi
done
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent >/dev/null 2>&1
netfilter-persistent save >/dev/null 2>&1 && echo "  rules persisted"

# ---------------------------------------------------------------------------------------
log "4/9  docker"
if command -v docker >/dev/null 2>&1; then
  echo "  already installed: $(docker --version)"
else
  curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 || die "docker install failed"
  echo "  $(docker --version)"
fi
systemctl enable --now docker >/dev/null 2>&1
usermod -aG docker ubuntu 2>/dev/null || true

# ---------------------------------------------------------------------------------------
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
# Every secret is >=32 chars because the production hardening check requires it of all
# four -- including the object-storage ACCESS key, which MinIO itself would accept much
# shorter. Hex, so nothing needs escaping in the DATABASE_URL.
log "6/9  configuration"
if [ -f .env.production ]; then
  echo "  .env.production exists -- keeping it (delete it to regenerate)"
else
  PGPASS=$(openssl rand -hex 24)
  cp .env.production.example .env.production
  sed -i \
    -e "s|^SITE_ADDRESS=.*|SITE_ADDRESS=:80|" \
    -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PGPASS}|" \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+psycopg://jobplatform:${PGPASS}@postgres:5432/jobplatform|" \
    -e "s|^OBJECT_STORAGE_ACCESS_KEY=.*|OBJECT_STORAGE_ACCESS_KEY=$(openssl rand -hex 24)|" \
    -e "s|^OBJECT_STORAGE_SECRET_KEY=.*|OBJECT_STORAGE_SECRET_KEY=$(openssl rand -hex 24)|" \
    -e "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 48)|" \
    -e "s|^JWT_SECRET=.*|JWT_SECRET=$(openssl rand -hex 48)|" \
    -e "s|^CORS_ALLOW_ORIGINS=.*|CORS_ALLOW_ORIGINS=https://localhost|" \
    -e "s|^INGEST_CATEGORY_ALLOWLIST=.*|INGEST_CATEGORY_ALLOWLIST=data-engineering,software|" \
    .env.production
  chmod 600 .env.production
  grep -qE '^[A-Z_]+=.*(CHANGE_ME|REPLACE-WITH)' .env.production \
    && die "placeholders survived generation" || echo "  generated, no placeholders left"
fi

C="docker compose -f infra/docker/docker-compose.prod.yml --env-file .env.production"

# ---------------------------------------------------------------------------------------
log "7/9  build and start  (slowest step -- the web build is the memory peak)"
$C up -d --build || die "stack failed to come up -- check: $C logs"
sleep 20
$C ps

# ---------------------------------------------------------------------------------------
log "8/9  database schema"
$C run --rm --no-deps ingest \
  python -m alembic -c database/migrations/alembic.ini upgrade head || die "migration failed"

# ---------------------------------------------------------------------------------------
# A fresh database has no checkpoints, so discover() treats every file the source ever
# published as pending -- roughly 80 files and about an hour. INGEST_SINCE bounds it; the
# daily timers then collect whatever was skipped.
log "9/9  first ingest"
if [ -n "$INGEST_SINCE" ]; then
  echo "  bounded to files on or after $INGEST_SINCE"
  $C run --rm ingest python scripts/ingest.py --trigger MANUAL --since "$INGEST_SINCE"
else
  echo "  full backfill -- expect roughly an hour"
  COMPOSE=1 ./scripts/daily.sh
fi

log "schedule"
./scripts/install-systemd.sh && systemctl list-timers 'jobplatform-*' --no-pager

IP=$(curl -s --max-time 10 ifconfig.me || echo "<server-ip>")
log "DONE"
echo "  http://${IP}/"
echo "  http://${IP}/admin"
