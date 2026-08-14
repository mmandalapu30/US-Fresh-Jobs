#!/usr/bin/env bash
# One daily command: pull new jobs, then drop anything that has aged out.
#
# On a server this is invoked by systemd timers (or cron) against the production stack:
#
#   09:00 America/New_York   ./scripts/daily.sh              primary run, ingest + retention
#   11,13,15,17 same zone    ./scripts/daily.sh --catch-up   ingest only, if the day is still missing
#
# 09:00 Eastern is 13:00 UTC (14:00 while the US is on standard time). The source
# publishes each day's file between roughly 06:30 and 14:15 UTC and the hour genuinely
# varies, so a single morning slot lands before the file exists on a fair share of days.
# The catch-up passes close that gap within the same day. They are cheap: each asks one
# indexed question of the database and stops there once the day's file is in.
#
# A missed or early run self-heals regardless -- discover() diffs the remote listing
# against our checkpoints, so nothing is skipped forever.
#
# Set COMPOSE=1 to run inside the production stack instead of on the host directly.
set -uo pipefail
cd "$(dirname "$0")/.."

# --catch-up runs the ingest only, and only when today's file is still missing. Retention
# is a once-a-day decision and stays with the primary run: repeating it four times could
# not remove anything the first pass had not already removed, and would bury the one log
# line that matters under three that never do anything.
catch_up=0
for arg in "$@"; do
  case "$arg" in
    --catch-up) catch_up=1 ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (expected --catch-up)" >&2; exit 64 ;;
  esac
done

label="daily"
[ "$catch_up" = "1" ] && label="catch-up"

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC')  $*"; }

COMPOSE_FILE="infra/docker/docker-compose.prod.yml"
run_step() {
  if [ "${COMPOSE:-0}" = "1" ]; then
    docker compose -f "$COMPOSE_FILE" --env-file .env.production \
      run --rm --no-deps ingest python "$@"
  else
    python "$@"
  fi
}

log "=== $label run starting ==="

# 0. Catch-up guard. Exit 0 means the day is already done, and re-checking the remote
#    listing to learn that would cost a network round trip and a sync_runs row per pass.
#
#    Exit 2 -- "could not tell" -- deliberately falls through to the ingest. An unknown
#    answer must never turn into a skipped day, and the ingest is idempotent, so the
#    worst case of guessing wrong here is one wasted directory listing.
if [ "$catch_up" = "1" ]; then
  run_step scripts/have_todays_file.py
  case $? in
    0) log "today's file is already in - nothing to do"
       log "=== $label run finished (skipped) ==="
       exit 0 ;;
    2) log "could not confirm today's file - ingesting anyway" ;;
  esac
fi

# 1. Pull whatever the source has published since we last looked.
#
# Retried, because the source resets connections and times out reads often enough to end a
# run on its own (about 1 request in 8 from one observed host). Retrying resumes rather
# than redoing: sync_files checkpoints every completed file, so a second attempt skips
# what already succeeded.
attempts=3
for attempt in $(seq 1 $attempts); do
  log "ingesting (attempt $attempt of $attempts)..."
  run_step scripts/ingest.py --trigger SCHEDULED
  code=$?

  if [ $code -eq 0 ]; then
    break
  fi

  # 3 means another ingest holds the per-source lock. Retrying cannot help: an abandoned
  # run is only reclaimed after 120 minutes. Not an error either -- the guard did its job.
  # This is the ordinary outcome when a slow primary run overlaps the first catch-up.
  if [ $code -eq 3 ]; then
    log "another ingest is already running - nothing to do"
    log "=== $label run finished (skipped) ==="
    exit 0
  fi

  if [ $attempt -eq $attempts ]; then
    log "=== $label run FAILED: ingest failed after $attempts attempts (exit $code) ==="
    exit 1
  fi

  wait_for=$((60 * attempt))
  log "ingest failed (exit $code) - retrying in ${wait_for}s"
  sleep $wait_for
done

if [ "$catch_up" = "1" ]; then
  log "=== $label run finished OK ==="
  exit 0
fi

# 2. Retire jobs past the freshness window, and trim old rejection records.
#
# --expire-only, not deletion. The platform's premise is that every qualifying job it has
# ever seen is preserved; the UI filters on status, so marking a job EXPIRED takes it off
# the board while keeping the record. Deleting would take the board and the history with
# it, and the history is the part that cannot be re-fetched -- the source rewrites and
# removes files. Pass --execute without --expire-only by hand if you genuinely want rows
# gone.
#
# Exit 2 means retention is switched off (RETENTION_MAX_POSTED_AGE_DAYS=0, the shipped
# default). Failing the run over it would report a failure after a perfectly good ingest.
log "enforcing retention..."
run_step scripts/enforce_retention.py --execute --expire-only --yes
code=$?
if [ $code -eq 2 ]; then
  log "retention disabled (RETENTION_MAX_POSTED_AGE_DAYS=0) - skipped"
elif [ $code -ne 0 ]; then
  log "=== $label run FAILED: retention exited $code ==="
  exit 1
fi

log "=== $label run finished OK ==="
