#!/usr/bin/env bash
#
# Git-driven deploy. Run on the server, from the repo root:
#
#   ./infra/deploy/deploy.sh                  # deploy the tip of the current branch
#   ./infra/deploy/deploy.sh --ref v1.2.0     # deploy a specific tag or commit
#   ./infra/deploy/deploy.sh --check          # show what would change, then stop
#   ./infra/deploy/deploy.sh --rollback       # restore the previously deployed images
#
# A deploy here is a rebuild, not an edit: containers run the code baked into the image,
# so nothing changes until an image is rebuilt. This script does that and then refuses to
# leave a broken stack serving -- if the new containers do not become healthy and answer
# over HTTP, it puts the previous images back automatically.
#
# What it cannot undo is a migration. Schema changes are one-way here by policy, because
# verifying reversibility against a populated production database once dropped two columns
# and wiped 182,766 classifications. Rollback restores containers, never the database, so a
# release whose migration is wrong needs a restore from backup rather than this script.
set -uo pipefail

COMPOSE_FILE="infra/docker/docker-compose.prod.yml"

# Per-host deployment settings, untracked, written once when the host is set up.
# A host running from prebuilt images sets COMPOSE_OVERLAY and IMAGE_TAG here, so that
# `./infra/deploy/deploy.sh` with no arguments does the right thing rather than depending
# on whoever runs it remembering to export two variables.
[ -f ./.deploy.conf ] && . ./.deploy.conf
COMPOSE_OVERLAY="${COMPOSE_OVERLAY:-}"
ENV_FILE=".env.production"
HEALTH_URL="${HEALTH_URL:-http://localhost/}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
LOG="${DEPLOY_LOG:-/var/log/jobplatform-deploy.log}"
IMAGES="jobplatform-prod-api jobplatform-prod-web jobplatform-prod-ingest"

ref=""
mode="deploy"
while [ $# -gt 0 ]; do
  case "$1" in
    --ref)      ref="${2:-}"; shift 2 ;;
    --check)    mode="check";    shift ;;
    --rollback) mode="rollback"; shift ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

ts()  { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts)  $*" | tee -a "$LOG" 2>/dev/null || echo "$(ts)  $*"; }
die() { log "FAILED: $*"; exit 1; }

COMPOSE_ARGS="-f $COMPOSE_FILE"
[ -n "$COMPOSE_OVERLAY" ] && COMPOSE_ARGS="$COMPOSE_ARGS -f $COMPOSE_OVERLAY"
# shellcheck disable=SC2086  # a list of arguments, not one argument
C="docker compose $COMPOSE_ARGS --env-file $ENV_FILE"

[ -f "$COMPOSE_FILE" ] || die "run this from the repo root ($COMPOSE_FILE not found)"
[ -f "$ENV_FILE" ]     || die "$ENV_FILE is missing -- see docs/07-deployment.md section 2"

# ---------------------------------------------------------------------------------------
# Rollback restores the images tagged during the previous deploy. It deliberately leaves
# git alone: the working tree is evidence of what was deployed, and rewriting it would
# destroy the ability to diff what broke.
# ---------------------------------------------------------------------------------------
if [ "$mode" = "rollback" ]; then
  for img in $IMAGES; do
    docker image inspect "${img}:rollback" >/dev/null 2>&1 \
      || die "no ${img}:rollback image -- nothing to roll back to"
  done
  log "=== rolling back to the previously deployed images ==="
  for img in $IMAGES; do
    docker tag "${img}:rollback" "${img}:latest"
  done
  $C up -d --no-build || die "rollback failed to start containers"
  log "rolled back. NOTE: any migration this release applied is still applied."
  exit 0
fi

# ---------------------------------------------------------------------------------------
log "=== deploy starting ==="
current="$(git rev-parse HEAD)"
branch="$(git rev-parse --abbrev-ref HEAD)"
log "current: ${current:0:8} on $branch"

# A dirty tree means someone edited the server by hand. Deploying over it would either
# discard their change silently or build an image from a state that exists in no commit.
if [ -n "$(git status --porcelain)" ]; then
  log "working tree is not clean:"
  git status --short | tee -a "$LOG"
  die "commit, stash or discard these first -- refusing to build an unversioned image"
fi

git fetch -q origin || die "git fetch failed"
target="$(git rev-parse "${ref:-origin/$branch}" 2>/dev/null)" \
  || die "cannot resolve ${ref:-origin/$branch}"
log "target:  ${target:0:8} ${ref:+($ref)}"

if [ "$current" = "$target" ]; then
  log "already at the target commit -- nothing to deploy"
  exit 0
fi

# ---------------------------------------------------------------------------------------
log "--- changes in this deploy ---"
git --no-pager log --oneline "$current..$target" 2>/dev/null | head -20 | tee -a "$LOG" \
  || log "(target is not a descendant of HEAD)"

migrations="$(git diff --name-only "$current" "$target" -- database/migrations/versions/ 2>/dev/null)"
if [ -n "$migrations" ]; then
  log "--- this release ADDS MIGRATIONS ---"
  echo "$migrations" | sed 's/^/    /' | tee -a "$LOG"
  log "    one-way: rollback restores containers, not the schema"
fi

if [ "$mode" = "check" ]; then
  log "--check given -- stopping before any change"
  exit 0
fi

# ---------------------------------------------------------------------------------------
# Tag what is running now, before touching git, so a build failure leaves the images and
# the working tree consistent with each other.
log "--- tagging current images as :rollback ---"
for img in $IMAGES; do
  if docker image inspect "${img}:latest" >/dev/null 2>&1; then
    docker tag "${img}:latest" "${img}:rollback"
    log "    ${img}:latest -> :rollback"
  else
    log "    ${img}: no current image (first deploy)"
  fi
done

log "--- updating source ---"
if ! git merge --ff-only "$target" >/dev/null 2>&1; then
  log "not a fast-forward -- checking out $target detached instead"
  git checkout -q --detach "$target" || die "checkout failed"
fi
log "now at $(git rev-parse --short HEAD)"

# ---------------------------------------------------------------------------------------
if [ -n "$COMPOSE_OVERLAY" ]; then
  # This host runs published images. Building here would ignore what CI produced and,
  # on a small box, may not fit in memory at all.
  log "--- pulling images (tag ${IMAGE_TAG:-latest}) ---"
  $C pull || die "pull failed -- is the image published for tag ${IMAGE_TAG:-latest}?"
else
  log "--- building ---"
  $C build || die "build failed -- nothing was replaced, the old stack is still serving"
fi

if [ -n "$migrations" ]; then
  log "--- applying migrations ---"
  $C run --rm --no-deps ingest \
    python -m alembic -c database/migrations/alembic.ini upgrade head \
    || die "migration failed -- old containers still serving, schema may be partial"
fi

log "--- starting new containers ---"
$C up -d || die "containers failed to start"

# ---------------------------------------------------------------------------------------
# Health gate. `up -d` exiting 0 only means Docker accepted the containers; it says nothing
# about whether the application works. Nothing gets past this point unless it is serving.
log "--- waiting for health (up to ${HEALTH_TIMEOUT}s) ---"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT ))
healthy=0
code="000"
while [ "$(date +%s)" -lt "$deadline" ]; do
  notready="$(docker ps --filter label=com.docker.compose.project=jobplatform-prod \
               --format '{{.Status}}' | grep -ci 'unhealthy\|health: starting' || true)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$HEALTH_URL" || echo 000)"
  if [ "$notready" -eq 0 ] && [ "$code" = "200" ]; then
    healthy=1
    break
  fi
  sleep 5
done

if [ "$healthy" -ne 1 ]; then
  log "!!! not healthy after ${HEALTH_TIMEOUT}s (last HTTP $code) -- rolling back"
  docker ps --format 'table {{.Names}}\t{{.Status}}' | tee -a "$LOG"
  $C logs --tail 40 api web 2>&1 | tail -60 | tee -a "$LOG"
  for img in $IMAGES; do
    docker image inspect "${img}:rollback" >/dev/null 2>&1 && docker tag "${img}:rollback" "${img}:latest"
  done
  git checkout -q "$current" 2>/dev/null || true
  if $C up -d --no-build; then
    log "rolled back to ${current:0:8}"
  else
    log "ROLLBACK ALSO FAILED -- manual intervention needed"
  fi
  if [ -n "$migrations" ]; then
    log "WARNING: migrations from the failed release remain applied"
  fi
  exit 1
fi

log "--- healthy ---"
docker ps --format 'table {{.Names}}\t{{.Status}}' | tee -a "$LOG"
log "=== deployed ${target:0:8} successfully ==="
