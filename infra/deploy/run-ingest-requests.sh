#!/usr/bin/env bash
#
# Claim and run on-demand fetch requests queued from the admin console.
#
#   ./infra/deploy/run-ingest-requests.sh            # claim one, run it, exit
#   ./infra/deploy/run-ingest-requests.sh --check    # report only
#
# Runs on the host from a one-minute timer. The API queues a row and this executes it,
# which is what keeps the web tier unprivileged: it never needs a Docker socket, and the
# worst a compromised API could ask for here is a bounded ingest that was going to run
# tonight anyway.
#
# Shell rather than Python, talking to postgres through `docker exec`, because the host has
# no Python environment -- the application's dependencies live in images. The backup and
# freshness scripts work the same way for the same reason.
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1

CONTAINER="${PG_CONTAINER:-jobplatform-postgres}"
DB="${POSTGRES_DB:-jobplatform}"
USER_="${POSTGRES_USER:-jobplatform}"
COMPOSE_OVERLAY="${COMPOSE_OVERLAY:-}"
IMAGE_TAG="${IMAGE_TAG:-}"
export IMAGE_TAG

q() { docker exec "$CONTAINER" psql -U "$USER_" -d "$DB" -tAc "$1" 2>/dev/null; }
ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || { echo "$(ts)  $CONTAINER not running"; exit 0; }

# Reclaim a request whose runner died. Without this a killed run leaves RUNNING forever and
# the console's button stays stuck on "in progress" with nothing behind it.
q "UPDATE ingest_requests
      SET status = 'FAILED', finished_at = now(),
          message = 'The runner stopped before finishing.'
    WHERE status = 'RUNNING' AND started_at < now() - interval '90 minutes'" >/dev/null

PENDING=$(q "SELECT id || ' ' || max_files FROM ingest_requests
              WHERE status = 'QUEUED' ORDER BY id LIMIT 1")
[ -z "$PENDING" ] && { [ "${1:-}" = "--check" ] && echo "nothing queued"; exit 0; }

REQ_ID="${PENDING%% *}"
MAX_FILES="${PENDING##* }"
[ "${1:-}" = "--check" ] && { echo "request $REQ_ID queued ($MAX_FILES files)"; exit 0; }

# Conditional on QUEUED, so two overlapping timer firings cannot both take the same
# request -- the second updates nothing. The run it would start twice takes minutes and
# holds a per-source lock, so this matters more than it looks.
CLAIMED=$(q "UPDATE ingest_requests SET status = 'RUNNING', started_at = now()
              WHERE id = $REQ_ID AND status = 'QUEUED' RETURNING id")
[ -z "$CLAIMED" ] && { echo "$(ts)  another runner claimed $REQ_ID"; exit 0; }

echo "$(ts)  running request $REQ_ID (max $MAX_FILES files)"

C=(docker compose -f infra/docker/docker-compose.prod.yml)
[ -n "$COMPOSE_OVERLAY" ] && C+=(-f "$COMPOSE_OVERLAY")
C+=(--env-file .env.production --profile ingest)

"${C[@]}" run --rm --no-deps ingest \
  python scripts/ingest.py --trigger MANUAL --max-files "$MAX_FILES" \
  > "/var/log/ingest-request-$REQ_ID.log" 2>&1
code=$?

# Exit 3 is the pipeline declining because another ingest holds the lock. That is not a
# failure of this request -- the data is being fetched by the run already in flight -- and
# calling it one would send an administrator hunting a fault that does not exist.
case "$code" in
  0) STATUS=SUCCEEDED; MSG="Fetch completed." ;;
  3) STATUS=SKIPPED;   MSG="Another ingest was already running." ;;
  *) STATUS=FAILED
     DETAIL=$(grep -vE '"logger": "httpx"' "/var/log/ingest-request-$REQ_ID.log" 2>/dev/null | tail -1 | cut -c1-200)
     MSG="Fetch failed (exit $code). ${DETAIL:-see /var/log/ingest-request-$REQ_ID.log}" ;;
esac

q "UPDATE ingest_requests
      SET status = '$STATUS', finished_at = now(),
          message = \$msg\$$MSG\$msg\$,
          sync_run_id = (SELECT max(id) FROM sync_runs)
    WHERE id = $REQ_ID" >/dev/null

echo "$(ts)  request $REQ_ID: $STATUS -- $MSG"
[ "$STATUS" = "FAILED" ] && exit 1
exit 0
