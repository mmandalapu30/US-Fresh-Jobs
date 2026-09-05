#!/usr/bin/env bash
#
# Keep asking Oracle for an Always Free ARM instance until one is granted.
#
#   ./infra/oracle/launch-retry.sh              # retry forever, 5 min apart
#   ./infra/oracle/launch-retry.sh --once       # one pass over every AD, then stop
#   INTERVAL=120 ./infra/oracle/launch-retry.sh # retry faster
#
# Always Free A1 capacity is heavily oversubscribed, so `Out of host capacity` is the
# normal answer, not an error to debug. It frees up unpredictably as other tenants
# release instances -- which makes this a polling problem, and the console has no poll.
#
# The script rotates through every availability domain in the region on each pass: they
# are separate capacity pools, so AD-1 being full says nothing about AD-2.
#
# Requires: the OCI CLI, configured (~/.oci/config). See infra/oracle/README.md.
set -uo pipefail

INTERVAL="${INTERVAL:-300}"
DISPLAY_NAME="${DISPLAY_NAME:-jobplatform}"
# A ladder, largest first. Capacity is fragmented: a host with no room for 4 OCPU often
# has room for 1, so asking for a single fixed size refuses capacity that exists. Every
# rung sits inside the Always Free allocation (4 OCPU / 24 GB of A1), so none of them
# starts billing -- and a small instance that landed can be resized later, whereas an
# instance that never launched cannot.
SHAPE_LADDER="${SHAPE_LADDER:-4:24 2:12 1:6}"
BOOT_GB="${BOOT_GB:-150}"
SHAPE="VM.Standard.A1.Flex"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/oracle_jobplatform.pub}"

once=0
# Anything unrecognised must stop, not fall through to the default. This read only $1 and
# ignored the rest, so `launch-retry.sh --help` -- or a typo -- silently meant "retry
# forever", which for this script is provisioning, not a no-op. Same shape as deploy.sh.
while [ $# -gt 0 ]; do
  case "$1" in
    --once)     once=1; shift ;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 64 ;;
  esac
done

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC')  $*"; }
die() { log "ERROR: $*"; exit 1; }

command -v oci >/dev/null 2>&1 || die "the OCI CLI is not installed -- see infra/oracle/README.md"
[ -f "$SSH_KEY_FILE" ] || die "no ssh public key at $SSH_KEY_FILE"
SSH_KEY="$(cat "$SSH_KEY_FILE")"

# ---- discover everything we can, so the only thing to configure is the CLI itself ----

# Read the tenancy straight out of the CLI's own config. Deriving it from a compartment
# listing needs a compartment to already exist and returns nothing on a fresh tenancy --
# which then surfaces as an opaque InvalidParameter from the next call, not as a clear
# error here.
OCI_CONFIG="${OCI_CONFIG_FILE:-$HOME/.oci/config}"
TENANCY="${TENANCY_OCID:-$(awk -F= '/^[[:space:]]*tenancy[[:space:]]*=/{gsub(/[[:space:]]/,"",$2); print $2; exit}' "$OCI_CONFIG" 2>/dev/null)}"
[ -n "$TENANCY" ] || die "no tenancy OCID in $OCI_CONFIG -- see infra/oracle/README.md step 4"
log "tenancy:  $TENANCY"

mapfile -t ADS < <(oci iam availability-domain list --compartment-id "$TENANCY" \
  --query 'data[].name' --raw-output 2>/dev/null | tr -d '[]," ' | grep -v '^$')
[ "${#ADS[@]}" -gt 0 ] || die "no availability domains returned"
log "ADs:      ${ADS[*]}"

IMAGE="${IMAGE_OCID:-$(oci compute image list --compartment-id "$TENANCY" \
  --operating-system 'Canonical Ubuntu' --operating-system-version '24.04' \
  --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
  --query 'data[0].id' --raw-output 2>/dev/null)}"
[ -n "$IMAGE" ] || die "no Ubuntu 24.04 aarch64 image found for $SHAPE"
log "image:    $IMAGE"

# A subnet must already exist. Creating a VCN here would be the script quietly making
# network decisions on your behalf; the console's default VCN wizard is the better place.
SUBNET="${SUBNET_OCID:-$(oci network subnet list --compartment-id "$TENANCY" \
  --query 'data[0].id' --raw-output 2>/dev/null)}"
[ -n "$SUBNET" ] && [ "$SUBNET" != "null" ] || die \
  "no subnet found. Create a VCN first: Networking > Virtual Cloud Networks > Start VCN Wizard"
log "subnet:   $SUBNET"
log "shape:    $SHAPE  ladder [$SHAPE_LADDER]  ${BOOT_GB} GB boot"

attempt=0
consecutive_unknown=0
while :; do
  for ad in "${ADS[@]}"; do
   for rung in $SHAPE_LADDER; do
    OCPUS="${rung%%:*}"; MEMORY_GB="${rung##*:}"
    attempt=$((attempt + 1))
    log "attempt $attempt in $ad  (${OCPUS} OCPU / ${MEMORY_GB} GB) ..."

    out=$(oci compute instance launch \
      --availability-domain "$ad" \
      --compartment-id "$TENANCY" \
      --shape "$SHAPE" \
      --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEMORY_GB}" \
      --image-id "$IMAGE" \
      --subnet-id "$SUBNET" \
      --assign-public-ip true \
      --display-name "$DISPLAY_NAME" \
      --boot-volume-size-in-gbs "$BOOT_GB" \
      --metadata "{\"ssh_authorized_keys\":\"$SSH_KEY\"}" \
      --wait-for-state RUNNING 2>&1)
    code=$?

    if [ $code -eq 0 ]; then
      id=$(echo "$out" | grep -o '"id": "[^"]*"' | head -1 | cut -d'"' -f4)
      log "GRANTED -- instance $id is RUNNING (${OCPUS} OCPU / ${MEMORY_GB} GB)"
      ip=$(oci compute instance list-vnics --instance-id "$id" \
             --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
      log "public IP: $ip"
      log "connect:   ssh -i ${SSH_KEY_FILE%.pub} ubuntu@$ip"
      exit 0
    fi

    # Three classes of failure, and conflating them is what makes a retry loop useless.
    #
    #  * capacity  -- the expected answer; ask again later.
    #  * fatal     -- auth, bad parameters, service limits. Retrying cannot fix any of
    #                 them and looping just buries the message.
    #  * transient -- endpoint timeouts, resets, throttling. Observed on a real run: AD-3
    #                 timed out mid-sweep while AD-1 and AD-2 answered normally. Treating
    #                 these as fatal ends an overnight wait on a blip.
    #
    # Fatal is tested BEFORE transient, and the HTTP statuses are matched only in the
    # ServiceError "status" field. Bare 429|50[023] matched any three digits anywhere in
    # the output, and OCI echoes the request -- so a NotAuthorized whose subnet OCID
    # happened to contain "503" read as a blip and retried every five minutes forever,
    # which is precisely the outcome this classification exists to prevent.
    if echo "$out" | grep -qi 'out of host capacity\|out of capacity'; then
      log "  no capacity in $ad at ${OCPUS} OCPU / ${MEMORY_GB} GB"
      consecutive_unknown=0
    elif echo "$out" | grep -qiE 'NotAuthenticated|NotAuthorized|InvalidParameter|LimitExceeded|QuotaExceeded|CannotParseRequest'; then
      log "fatal -- retrying cannot fix this:"
      echo "$out" >&2
      exit 1
    elif echo "$out" | grep -qiE 'timed out|timeout|connection (reset|aborted|refused)|RequestException|TooManyRequests|ServiceUnavailable|InternalServerError' ||
         echo "$out" | grep -qE '"status":[[:space:]]*(429|500|502|503)'; then
      log "  transient error in $ad (will retry): $(echo "$out" | grep -ioE 'The connection to endpoint timed out|[A-Za-z]*(TimeoutError|ConnectionError|TooManyRequests)' | head -1)"
      consecutive_unknown=0
    else
      # Unrecognised. Do not exit on the first one: a novel transient error would end the
      # wait. Do not loop forever either -- give up once it is clearly not going away.
      consecutive_unknown=$((consecutive_unknown + 1))
      log "  unrecognised failure in $ad ($consecutive_unknown/6):"
      echo "$out" | head -12 >&2
      if [ $consecutive_unknown -ge 6 ]; then
        log "six unrecognised failures in a row -- stopping so the message is not buried"
        exit 1
      fi
    fi
   done
  done

  [ $once -eq 1 ] && { log "--once given, stopping after one pass"; exit 2; }
  log "all ADs full; sleeping ${INTERVAL}s"
  sleep "$INTERVAL"
done
