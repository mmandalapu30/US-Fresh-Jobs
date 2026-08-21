#!/usr/bin/env bash
# One daily command: pull new jobs, then drop anything that has aged out.
#
# On a server this is invoked by systemd timers (or cron) against the production stack:
#
#   09:00 America/New_York   ./scripts/daily.sh              primary run, ingest + retention
#   every 10 minutes         ./scripts/daily.sh --watch      ingest only, when a new file is published
#   on demand                ./scripts/daily.sh --catch-up   ingest only, if today's file is still missing
#
# The source publishes each day's file between roughly 06:30 and 14:15 UTC and the hour
# genuinely varies, so no calendar slot is the right one: an early slot runs before the
# file exists, a late slot leaves the day's jobs unfetched for hours. --watch asks the
# source itself instead -- scripts/has_new_file.py, one directory listing -- and ingests
# within a timer interval of publication. It supersedes the fixed afternoon catch-up
# slots; --catch-up remains for a manual re-check of today specifically.
#
# 09:00 Eastern is 13:00 UTC, 14:00 while the US is on standard time. The primary run
# stays on the calendar because retention is a once-a-day decision rather than a
# reaction to a file landing.
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
watch=0
for arg in "$@"; do
  case "$arg" in
    --catch-up) catch_up=1 ;;
    --watch)    watch=1 ;;
    -h|--help)  sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg (expected --watch or --catch-up)" >&2; exit 64 ;;
  esac
done

label="daily"
[ "$catch_up" = "1" ] && label="catch-up"
[ "$watch" = "1" ] && label="watch"

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC')  $*"; }

COMPOSE_FILE="infra/docker/docker-compose.prod.yml"

# An optional overlay applied on top of the production file. A host running the stack
# from prebuilt images needs it here too: without it this script sees only the production
# file, where the ingest service carries a `build:` and no `image:` -- so every scheduled
# run rebuilds from source instead of pulling what CI already published. Observed doing
# exactly that on the first deploy. Set COMPOSE_OVERLAY in the unit file on such a host.
COMPOSE_OVERLAY="${COMPOSE_OVERLAY:-}"
compose_args() {
  printf %s "-f $COMPOSE_FILE"
  [ -n "$COMPOSE_OVERLAY" ] && printf %s " -f $COMPOSE_OVERLAY"
  return 0
}

run_step() {
  if [ "${COMPOSE:-0}" = "1" ]; then
    # Unquoted deliberately: this expands to a list of arguments, not a single one.
    # shellcheck disable=SC2046
    docker compose $(compose_args) --env-file .env.production \
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

# 0b. Watch guard. Asks the source what it has rather than the database what we took, so
#     a backfilled gap or a corrected republish counts as work even though today's file
#     is already in. Costs one directory listing per idle pass and, unlike running the
#     pipeline to find out, opens no sync run and takes no per-source lock.
#
#     Exit 2 falls through for the same reason it does above: unknown must mean ingest.
if [ "$watch" = "1" ]; then
  run_step scripts/has_new_file.py
  case $? in
    0) log "no new file at the source - nothing to do"
       log "=== $label run finished (skipped) ==="
       exit 0 ;;
    2) log "could not reach the source - ingesting anyway" ;;
  esac
fi

# 1. Pull whatever the source has published since we last looked.
#
# Bounded to the newest few files. discover() returns every delta file the source has ever
# published -- 59 were pending on this deployment -- and the pipeline walks them oldest
# first, so an unbounded run begins on a file from months back. That file is both the most
# expensive to process and the least useful: PURGE_AFTER_DAYS deletes anything posted more
# than a fortnight ago, so its rows are removed the same night they are inserted. It is
# also big enough to exhaust a small host, and an OOM-killed worker never finalises its
# sync_runs row -- the abandoned row then holds the per-source lock for the full 120-minute
# reclaim window and blocks the admin console's fetch button along with it. --max-files
# keeps the NEWEST N pending files, which is what "today's jobs" means here. Raise it (or
# call scripts/ingest.py --since directly) to backfill deliberately.
#
# Watch takes ONE file, not three, and that is the whole point of the distinction.
# --max-files selects the newest N pending, but the pipeline then walks the selection
# oldest-first, so on a host carrying a backlog the newest file is processed LAST. On
# 2026-08-21 that starved the only file anyone wanted: the day's own delta published at
# 14:17, the watch pass picked it up five minutes later, and the run went to 07-23
# (304 MB) and 07-24 (295 MB) first and was OOM-killed before it ever opened 08-21. The
# file was selected by every run that day and read by none of them.
#
# One file removes the starvation without reordering anything. Processing order has to
# stay oldest-first: the loader decides an update by comparing content_hash for
# inequality, with no timestamp guard, so replaying an older file after a newer one
# would overwrite the newer row with stale content rather than skip it.
#
# Backfill is the daily run's job, where three-at-a-time and oldest-first are both right.
if [ "$watch" = "1" ]; then
  INGEST_MAX_FILES="${INGEST_WATCH_MAX_FILES:-1}"
else
  INGEST_MAX_FILES="${INGEST_MAX_FILES:-3}"
fi
#
# Retried, because the source resets connections and times out reads often enough to end a
# run on its own (about 1 request in 8 from one observed host). Retrying resumes rather
# than redoing: sync_files checkpoints every completed file, so a second attempt skips
# what already succeeded.
attempts=3
for attempt in $(seq 1 $attempts); do
  log "ingesting (attempt $attempt of $attempts)..."
  run_step scripts/ingest.py --trigger SCHEDULED --max-files "$INGEST_MAX_FILES"
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

# Ingest-only modes stop here. Retention is a once-a-day decision and stays with the
# primary run: repeating it on every watch pass could not remove anything the first pass
# had not already removed, and would bury the one log line that matters.
if [ "$catch_up" = "1" ] || [ "$watch" = "1" ]; then
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
