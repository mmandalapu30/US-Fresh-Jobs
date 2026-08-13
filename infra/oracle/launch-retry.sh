#!/usr/bin/env bash
#
# Keep asking Oracle for an Always Free ARM instance until one is granted.
#
#   ./infra/oracle/launch-retry.sh              # retry forever, 5 min apart
#   ./infra/oracle/launch-retry.sh --once       # single attempt, for testing config
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
OCPUS="${OCPUS:-2}"
MEMORY_GB="${MEMORY_GB:-12}"
BOOT_GB="${BOOT_GB:-150}"
SHAPE="VM.Standard.A1.Flex"
SSH_KEY_FILE="${SSH_KEY_FILE:-$HOME/.ssh/oracle_jobplatform.pub}"

once=0
[ "${1:-}" = "--once" ] && once=1

log() { echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC')  $*"; }
die() { log "ERROR: $*"; exit 1; }

command -v oci >/dev/null 2>&1 || die "the OCI CLI is not installed -- see infra/oracle/README.md"
[ -f "$SSH_KEY_FILE" ] || die "no ssh public key at $SSH_KEY_FILE"
SSH_KEY="$(cat "$SSH_KEY_FILE")"

# ---- discover everything we can, so the only thing to configure is the CLI itself ----

TENANCY="${TENANCY_OCID:-$(oci iam compartment list --query 'data[0]."compartment-id"' --raw-output 2>/dev/null)}"
[ -n "$TENANCY" ] || die "could not determine the tenancy OCID -- is ~/.oci/config valid?"
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
log "shape:    $SHAPE  ${OCPUS} OCPU / ${MEMORY_GB} GB / ${BOOT_GB} GB boot"

attempt=0
while :; do
  for ad in "${ADS[@]}"; do
    attempt=$((attempt + 1))
    log "attempt $attempt in $ad ..."

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
      log "GRANTED -- instance $id is RUNNING"
      ip=$(oci compute instance list-vnics --instance-id "$id" \
             --query 'data[0]."public-ip"' --raw-output 2>/dev/null)
      log "public IP: $ip"
      log "connect:   ssh -i ${SSH_KEY_FILE%.pub} ubuntu@$ip"
      exit 0
    fi

    # Capacity is the expected failure and means "ask again later". Anything else is a
    # real misconfiguration and retrying it just hides the message, so stop.
    if echo "$out" | grep -qi 'out of host capacity\|out of capacity'; then
      log "  no capacity in $ad"
    else
      log "unexpected failure -- not a capacity problem, so not retrying:"
      echo "$out" >&2
      exit 1
    fi
  done

  [ $once -eq 1 ] && { log "--once given, stopping after one pass"; exit 2; }
  log "all ADs full; sleeping ${INTERVAL}s"
  sleep "$INTERVAL"
done
