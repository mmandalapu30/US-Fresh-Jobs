#!/usr/bin/env bash
# Install the ingest schedule as systemd timers. Linux equivalent of
# scripts/install-daily-task.ps1.
#
#   sudo ./scripts/install-systemd.sh            # install or update
#   sudo ./scripts/install-systemd.sh --remove   # uninstall
#   ./scripts/install-systemd.sh --dry-run       # print what would be written
#
# Two timers are installed:
#
#   jobplatform-daily     09:00 America/New_York   ingest + retention
#   jobplatform-watch     every 10 minutes         ingest only, when the source publishes
#
# Re-running is safe and is how you move the repo or change the slot: the units are
# rewritten from the templates in infra/systemd/ and the timers re-enabled.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$REPO/infra/systemd"
UNIT_DIR="/etc/systemd/system"
UNITS=(jobplatform-daily.service jobplatform-daily.timer
       jobplatform-watch.service jobplatform-watch.timer
       jobplatform-backup.service jobplatform-backup.timer
       jobplatform-freshness.service jobplatform-freshness.timer
       jobplatform-ingest-requests.service jobplatform-ingest-requests.timer
       jobplatform-purge.service jobplatform-purge.timer)
TIMERS=(jobplatform-daily.timer jobplatform-watch.timer
        jobplatform-backup.timer jobplatform-freshness.timer
        jobplatform-ingest-requests.timer jobplatform-purge.timer)

# Units this installer used to write and no longer does. Removed on every install, not
# only on --remove: a host that ran an older installer still has jobplatform-catchup.timer
# enabled on disk, and systemd would go on firing it alongside the watcher that replaced
# it -- two ingests racing for the same per-source lock, four times an afternoon.
LEGACY_UNITS=(jobplatform-catchup.service jobplatform-catchup.timer)
LEGACY_TIMERS=(jobplatform-catchup.timer)

remove=0
dry_run=0
for arg in "$@"; do
  case "$arg" in
    --remove)  remove=1 ;;
    --dry-run) dry_run=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 64 ;;
  esac
done

need_root() {
  if [ "$(id -u)" -ne 0 ] && [ "$dry_run" -eq 0 ]; then
    echo "This writes to $UNIT_DIR -- re-run with sudo." >&2
    exit 1
  fi
}

purge_legacy() {
  for timer in "${LEGACY_TIMERS[@]}"; do
    [ -e "$UNIT_DIR/$timer" ] || continue
    if [ "$dry_run" -eq 1 ]; then
      echo "  would disable and remove superseded $timer"
    else
      systemctl disable --now "$timer" 2>/dev/null || true
    fi
  done
  for unit in "${LEGACY_UNITS[@]}"; do
    [ -e "$UNIT_DIR/$unit" ] || continue
    # The drop-in directory goes too. It carries COMPOSE_OVERLAY for the ingest units, and
    # an orphaned one left behind would be silently reused if the unit name ever returned.
    [ "$dry_run" -eq 1 ] && { echo "  would remove superseded $unit"; continue; }
    rm -rf "$UNIT_DIR/$unit" "$UNIT_DIR/$unit.d"
    echo "  removed superseded $unit"
  done
}

command -v systemctl >/dev/null 2>&1 || {
  echo "systemctl not found. This host does not use systemd; see docs/07-deployment.md §3 for the cron equivalent." >&2
  exit 1
}

if [ "$remove" -eq 1 ]; then
  need_root
  for timer in "${TIMERS[@]}"; do
    systemctl disable --now "$timer" 2>/dev/null || true
  done
  for unit in "${UNITS[@]}"; do
    rm -f "$UNIT_DIR/$unit"
  done
  purge_legacy
  systemctl daemon-reload
  echo "Removed the jobplatform timers."
  exit 0
fi

# Validate the schedule before installing it, not after the first silent no-show.
#
# Naming a timezone in OnCalendar= needs systemd 240+. Rather than assert a version, ask
# this systemd to parse the expressions we are about to install: it either understands
# them and prints the next elapse, or it does not and we stop here with the reason.
echo "Checking the schedule against this host's systemd..."
schedule_ok=1
while IFS= read -r spec; do
  if out=$(systemd-analyze calendar "$spec" 2>&1); then
    printf '  ok   %-42s %s\n' "$spec" "$(sed -n 's/^ *Next elapse: *//p' <<<"$out" | head -1)"
  else
    printf '  FAIL %-42s %s\n' "$spec" "$(head -1 <<<"$out")"
    schedule_ok=0
  fi
done < <(grep -h '^OnCalendar=' "$UNIT_SRC"/*.timer | sed 's/^OnCalendar=//')

if [ "$schedule_ok" -ne 1 ]; then
  cat >&2 <<'MSG'

The named timezone was not accepted. That means systemd is older than 240. Either
upgrade, or edit infra/systemd/*.timer to express each slot in UTC rather than naming a
zone. Eastern is UTC-4 under daylight saving and UTC-5 otherwise, so a fixed UTC slot is
correct for one half of the year and an hour out for the other.
MSG
  exit 1
fi

[ -x "$REPO/scripts/daily.sh" ] || {
  echo "scripts/daily.sh is not executable. Fix with: chmod +x scripts/daily.sh" >&2
  exit 1
}

need_root
purge_legacy
for unit in "${UNITS[@]}"; do
  # The templates carry /srv/job-platform as the documented default. Rewrite both path
  # directives so the units are correct wherever this checkout actually lives.
  rendered=$(sed -e "s|^WorkingDirectory=.*|WorkingDirectory=$REPO|" \
                 -e "s|^ExecStart=[^ ]*/scripts/daily.sh|ExecStart=$REPO/scripts/daily.sh|" \
                 -e "s|^ExecStart=[^ ]*/infra/backup/|ExecStart=$REPO/infra/backup/|" \
                 -e "s|^ExecStart=[^ ]*/infra/deploy/|ExecStart=$REPO/infra/deploy/|" \
                 -e "s|^Documentation=file://.*|Documentation=file://$REPO/docs/07-deployment.md|" \
                 "$UNIT_SRC/$unit")

  # A host running from prebuilt images needs COMPOSE_OVERLAY in the unit too, or the
  # scheduled run rebuilds from source nightly instead of pulling. Written as
  # a drop-in rather than edited into the unit: systemd merges drop-ins itself, so the
  # tracked template stays the tracked template and an upgrade cannot silently drop this.
  # Only the ingest units drive compose; backup and freshness talk to the running
  # postgres container directly and would gain nothing from the overlay.
  #
  # IMAGE_TAG is deliberately not written here. deploy.sh rewrites it in .env.production on
  # every deploy and rollback and compose reads it from there via --env-file, so a copy in
  # the drop-in only shadows it -- systemd's Environment= beats --env-file -- and nothing
  # ever refreshes the copy. Pinned that way the scheduled ingest keeps running whatever tag
  # was current at install time while api and web move on: observed running four commits
  # behind the rest of the stack. The fallback was worse than the pin, resolving to a branch
  # name rather than to a tag that exists.
  needs_overlay=0
  case "$unit" in
    jobplatform-daily.service|jobplatform-watch.service|jobplatform-ingest-requests.service)
      needs_overlay=1 ;;
  esac
  if [ "$needs_overlay" -eq 1 ] && [ -n "${COMPOSE_OVERLAY:-}" ]; then
    dropin="$UNIT_DIR/${unit}.d"
    if [ "$dry_run" -eq 1 ]; then
      echo "===== $dropin/overlay.conf ====="
      printf "[Service]\nEnvironment=COMPOSE_OVERLAY=%s\n" "$COMPOSE_OVERLAY"
    else
      mkdir -p "$dropin"
      printf "[Service]\nEnvironment=COMPOSE_OVERLAY=%s\n" "$COMPOSE_OVERLAY" > "$dropin/overlay.conf"
      echo "  wrote $dropin/overlay.conf"
    fi
  fi
  if [ "$dry_run" -eq 1 ]; then
    echo "===== $UNIT_DIR/$unit ====="
    echo "$rendered"
  else
    echo "$rendered" > "$UNIT_DIR/$unit"
    echo "  wrote $UNIT_DIR/$unit"
  fi
done

if [ "$dry_run" -eq 1 ]; then
  echo
  echo "Dry run -- nothing was written."
  exit 0
fi

systemctl daemon-reload
for timer in "${TIMERS[@]}"; do
  systemctl enable --now "$timer"
done

echo
systemctl list-timers 'jobplatform-*' --no-pager
cat <<MSG

Installed. The next primary run is listed above.

  logs        journalctl -u jobplatform-daily.service -f
  watcher     journalctl -u jobplatform-watch.service --since today
  run now     systemctl start jobplatform-daily.service
MSG
