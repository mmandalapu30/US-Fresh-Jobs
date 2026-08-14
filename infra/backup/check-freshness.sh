#!/usr/bin/env bash
#
# Is the board still being fed?
#
#   ./infra/backup/check-freshness.sh            # exit 0 fresh, 1 stale, 2 cannot tell
#   MAX_AGE_HOURS=48 ./infra/backup/check-freshness.sh
#   ALERT_WEBHOOK=https://... ./infra/backup/check-freshness.sh
#
# docs/07 §7 states the gap plainly: a failed timer writes to the journal, and nothing
# reads the journal. This reads it. It answers one question -- when did an ingest last
# SUCCEED -- because that is the only event that means data actually arrived. A run that
# started, or failed, or is still going, has fed the board nothing.
#
# Threshold is 26 hours, not 24: the source publishes daily but the upload hour moves
# across roughly 06:33-14:15 UTC, so two consecutive runs can legitimately sit more than a
# day apart. Alerting at 24 would cry wolf on a normal week.
set -uo pipefail

MAX_AGE_HOURS="${MAX_AGE_HOURS:-26}"
CONTAINER="${PG_CONTAINER:-jobplatform-postgres}"
DB="${POSTGRES_DB:-jobplatform}"
USER_="${POSTGRES_USER:-jobplatform}"
WEBHOOK="${ALERT_WEBHOOK:-}"

ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

alert() {
  echo "$(ts)  STALE: $*"
  # Optional and unset by default. Anything accepting a JSON POST works -- Slack, Discord,
  # ntfy, a webhook relay. Without one this still fails loudly enough for `systemctl
  # --failed` and the journal to show it.
  if [ -n "$WEBHOOK" ]; then
    curl -fsS -X POST -H 'Content-Type: application/json' \
      -d "{\"text\":\"US Fresh Jobs: $*\"}" "$WEBHOOK" >/dev/null 2>&1 \
      && echo "  alert sent" || echo "  alert delivery FAILED"
  fi
}

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || {
  alert "database container $CONTAINER is not running"
  exit 1
}

q() { docker exec "$CONTAINER" psql -U "$USER_" -d "$DB" -tAc "$1" 2>/dev/null; }

LAST=$(q "select extract(epoch from (now() - max(finished_at)))::bigint
            from sync_runs where status = 'SUCCEEDED'")

# An unreachable database is not the same as a stale one, and reporting it as stale would
# send someone looking at the wrong thing. Exit 2 says so.
if [ -z "$LAST" ]; then
  echo "$(ts)  UNKNOWN: cannot read sync_runs"
  exit 2
fi
if [ "$LAST" = "" ] || [ "$LAST" = "NULL" ]; then
  alert "no successful ingest has ever completed"
  exit 1
fi

AGE_H=$(( LAST / 3600 ))
JOBS=$(q "select count(*) from jobs where status = 'ACTIVE'")

if [ "$AGE_H" -gt "$MAX_AGE_HOURS" ]; then
  alert "last successful ingest was ${AGE_H}h ago (threshold ${MAX_AGE_HOURS}h), ${JOBS:-?} active jobs"
  exit 1
fi

echo "$(ts)  OK: last ingest ${AGE_H}h ago, ${JOBS:-?} active jobs"
exit 0
