#!/usr/bin/env bash
#
# Nightly database backup.
#
#   ./infra/backup/backup.sh              # dump, verify, prune
#   ./infra/backup/backup.sh --list       # what is currently held
#   ./infra/backup/backup.sh --verify F   # check one dump without restoring it
#
# Exists because migrations are one-way here. deploy.sh can put the previous containers
# back in under a minute, but nothing puts a column back -- verifying a migration reverses
# by running it against a populated production database once dropped two columns and wiped
# 182,766 classifications (docs/06 §6.5). So the backup is not belt-and-braces, it is the
# only recovery path for a bad schema change.
#
# Custom format, not plain SQL: pg_restore can then pick out a single table, and the dump
# is compressed without a second step.
set -uo pipefail

DIR="${BACKUP_DIR:-/var/backups/jobplatform}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
CONTAINER="${PG_CONTAINER:-jobplatform-postgres}"
DB="${POSTGRES_DB:-jobplatform}"
USER_="${POSTGRES_USER:-jobplatform}"

ts()  { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
log() { echo "$(ts)  $*"; }
die() { log "FAILED: $*"; exit 1; }

case "${1:-}" in
  --list)
    ls -lh "$DIR"/*.dump 2>/dev/null || echo "no backups in $DIR"
    exit 0 ;;
  --verify)
    f="${2:?usage: --verify <file>}"
    docker exec -i "$CONTAINER" pg_restore --list < "$f" >/dev/null 2>&1 \
      && { echo "$f: readable"; exit 0; } || { echo "$f: CORRUPT"; exit 1; }
    ;;
esac

mkdir -p "$DIR"
OUT="$DIR/jobplatform-$(date -u +%Y%m%d-%H%M%S).dump"

log "=== backup starting ==="
docker ps --format '{{.Names}}' | grep -qx "$CONTAINER" || die "$CONTAINER is not running"

# Dump straight out of the container to a host file. --no-owner keeps it restorable into a
# database owned by a different role, which is what a scratch restore target usually is.
if ! docker exec "$CONTAINER" pg_dump -U "$USER_" -d "$DB" --format=custom --no-owner > "$OUT" 2>/tmp/pgdump.err; then
  rm -f "$OUT"
  die "pg_dump failed: $(tail -3 /tmp/pgdump.err)"
fi

SIZE=$(stat -c%s "$OUT" 2>/dev/null || echo 0)
[ "$SIZE" -gt 1024 ] || { rm -f "$OUT"; die "dump is $SIZE bytes -- refusing to keep it"; }

# A dump that cannot be read back is not a backup. Reading the table of contents proves the
# archive header and object list survived, which is what catches a truncated write -- the
# common failure when a disk fills mid-dump.
if ! docker exec -i "$CONTAINER" pg_restore --list < "$OUT" >/tmp/pgrestore.list 2>/tmp/pgrestore.err; then
  die "dump is unreadable: $(tail -3 /tmp/pgrestore.err)"
fi
TABLES=$(grep -c "TABLE DATA" /tmp/pgrestore.list || echo 0)
[ "$TABLES" -gt 0 ] || die "dump contains no table data"

log "  wrote $(basename "$OUT")  ($(numfmt --to=iec "$SIZE" 2>/dev/null || echo "$SIZE bytes"), $TABLES tables)"

# Prune by age, then make sure pruning never empties the directory: a clock problem or a
# long outage should not leave you with nothing.
find "$DIR" -name 'jobplatform-*.dump' -mtime "+$KEEP_DAYS" -print -delete | while read -r f; do
  log "  pruned $(basename "$f")"
done
REMAINING=$(find "$DIR" -name 'jobplatform-*.dump' | wc -l)
[ "$REMAINING" -gt 0 ] || die "pruning left no backups -- this should be impossible"

log "=== backup finished: $REMAINING held, $KEEP_DAYS day retention ==="
