#!/usr/bin/env bash
#
# Permanently delete jobs older than the purge window.
#
#   ./infra/backup/purge-old-jobs.sh            # dry run — reports, deletes nothing
#   ./infra/backup/purge-old-jobs.sh --execute  # delete
#
# This is the one scheduled job in the system that destroys data. Everything else
# transitions state: retention marks jobs EXPIRED so they leave the board while the record
# survives. This removes the record.
#
# That is a deliberate choice about storage, not an accident, so the script is built to
# make it hard to regret:
#
#   * dry run by default, so a mistyped invocation reports instead of deleting
#   * refuses to run without a backup from the last 24 hours -- the deletion is
#     irreversible, and "restore from backup" is only an answer if a backup exists
#   * refuses if the window is small enough to delete jobs the board is still showing,
#     which would empty the site rather than tidy the database
#   * deletes in batches, so a large first run does not hold one long transaction
#
# PURGE_AFTER_DAYS must exceed RETENTION_MAX_POSTED_AGE_DAYS. Jobs are retired from the
# board at the retention window and removed from the database at this one; inverting them
# would delete jobs still being displayed.
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1

PURGE_AFTER_DAYS="${PURGE_AFTER_DAYS:-15}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/jobplatform}"
CONTAINER="${PG_CONTAINER:-jobplatform-postgres}"
DB="${POSTGRES_DB:-jobplatform}"
USER_="${POSTGRES_USER:-jobplatform}"
COMPOSE_OVERLAY="${COMPOSE_OVERLAY:-}"
IMAGE_TAG="${IMAGE_TAG:-}"
export IMAGE_TAG

EXECUTE=0
[ "${1:-}" = "--execute" ] && EXECUTE=1

ts()  { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts)  $*"; }
die() { log "REFUSING: $*"; exit 1; }

q() { docker exec "$CONTAINER" psql -U "$USER_" -d "$DB" -tAc "$1" 2>/dev/null; }

log "=== purge starting (window ${PURGE_AFTER_DAYS} days, $([ $EXECUTE -eq 1 ] && echo EXECUTE || echo "dry run")) ==="

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || die "$CONTAINER is not running"

# ---------------------------------------------------------------------------------------
# The window must sit outside what the board displays. Deleting inside the retention window
# would remove jobs visitors can currently see.
RETENTION_DAYS=$(grep -E '^RETENTION_MAX_POSTED_AGE_DAYS=' .env.production 2>/dev/null | cut -d= -f2 | tr -d ' ')
RETENTION_DAYS="${RETENTION_DAYS:-0}"
if [ "$RETENTION_DAYS" -gt 0 ] && [ "$PURGE_AFTER_DAYS" -le "$RETENTION_DAYS" ]; then
  die "purge window (${PURGE_AFTER_DAYS}d) must exceed the retention window (${RETENTION_DAYS}d)"
fi

# ---------------------------------------------------------------------------------------
# No backup, no deletion. This is the whole reason the two jobs are ordered the way they
# are in the timer: back up at 04:00, purge at 04:30.
if [ $EXECUTE -eq 1 ]; then
  RECENT=$(find "$BACKUP_DIR" -name 'jobplatform-*.dump' -mtime -1 2>/dev/null | head -1)
  [ -n "$RECENT" ] || die "no backup newer than 24h in $BACKUP_DIR -- run infra/backup/backup.sh first"
  log "  backup present: $(basename "$RECENT")"
fi

# ---------------------------------------------------------------------------------------
BEFORE_JOBS=$(q "SELECT count(*) FROM jobs")
BEFORE_ACTIVE=$(q "SELECT count(*) FROM jobs WHERE status = 'ACTIVE'")
CANDIDATES=$(q "SELECT count(*) FROM jobs
                 WHERE COALESCE(CASE WHEN posted_at_is_valid THEN posted_at END, first_seen_at)
                       < now() - interval '${PURGE_AFTER_DAYS} days'")
ACTIVE_AT_RISK=$(q "SELECT count(*) FROM jobs
                     WHERE status = 'ACTIVE'
                       AND COALESCE(CASE WHEN posted_at_is_valid THEN posted_at END, first_seen_at)
                           < now() - interval '${PURGE_AFTER_DAYS} days'")

log "  jobs in database:  ${BEFORE_JOBS}"
log "  active (on board): ${BEFORE_ACTIVE}"
log "  older than window: ${CANDIDATES}"
log "  of those, ACTIVE:  ${ACTIVE_AT_RISK}"

[ "${CANDIDATES:-0}" -eq 0 ] && { log "=== nothing to purge ==="; exit 0; }

if [ $EXECUTE -eq 0 ]; then
  log "dry run -- nothing deleted. Re-run with --execute to apply."
  exit 0
fi

# ---------------------------------------------------------------------------------------
# enforce_retention.py owns the age predicate and the batching, and now also clears the
# job_events rows that a delete would otherwise orphan -- that table is partitioned and has
# no foreign key to jobs, so nothing cascades on its behalf.
C=(docker compose -f infra/docker/docker-compose.prod.yml)
[ -n "$COMPOSE_OVERLAY" ] && C+=(-f "$COMPOSE_OVERLAY")
C+=(--env-file .env.production --profile ingest)

"${C[@]}" run --rm --no-deps ingest \
  python scripts/enforce_retention.py --days "$PURGE_AFTER_DAYS" --execute --yes \
  2>&1 | grep -vE '"logger"' | tail -12
code=${PIPESTATUS[0]}

AFTER_JOBS=$(q "SELECT count(*) FROM jobs")
AFTER_ACTIVE=$(q "SELECT count(*) FROM jobs WHERE status = 'ACTIVE'")
ORPHANS=$(q "SELECT count(*) FROM job_events e
              WHERE NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id = e.job_id)")

log "  jobs:   ${BEFORE_JOBS} -> ${AFTER_JOBS}  (removed $((BEFORE_JOBS - AFTER_JOBS)))"
log "  active: ${BEFORE_ACTIVE} -> ${AFTER_ACTIVE}"
log "  orphaned events remaining: ${ORPHANS:-?}"

# The board losing everything is the failure this is most likely to cause, and the one
# worth shouting about rather than leaving in a log nobody reads.
if [ "${AFTER_ACTIVE:-0}" -eq 0 ] && [ "${BEFORE_ACTIVE:-0}" -gt 0 ]; then
  log "WARNING: the board is now empty. Check PURGE_AFTER_DAYS against the retention window."
fi

log "=== purge finished (exit ${code}) ==="
exit "$code"
