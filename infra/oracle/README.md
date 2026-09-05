# Getting an Always Free ARM instance out of Oracle

`launch-retry.sh` exists because Always Free A1 capacity is heavily oversubscribed and the
console offers no way to wait for it. `Out of host capacity` is the normal answer, not a
misconfiguration — capacity is released unpredictably as other tenants give instances back,
so the only reliable approach is to keep asking.

The script rotates through every availability domain in the region on each pass, because
they are separate capacity pools: AD-1 being full says nothing about AD-2.

---

## 1. A VCN must exist first

The script will not create one — that would be it quietly making network decisions for you.

Console → **Networking → Virtual Cloud Networks → Start VCN Wizard** →
*VCN with Internet Connectivity* → accept the defaults.

While you are there, add the ingress rules the platform needs, because the instance is
useless without them: **Security Lists → Default Security List → Add Ingress Rules**,
source `0.0.0.0/0`, TCP, ports **80** and **443**.

## 2. Install the OCI CLI

```bash
bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)"
```

## 3. Register the API signing key

A key pair has already been generated at `~/.oci/oci_api_key.pem`. Upload its public half:

```bash
cat ~/.oci/oci_api_key_public.pem
```

Console → your avatar (top right) → **User settings** → **API keys** → **Add API key** →
*Paste a public key* → paste that block → **Add**.

Oracle then shows a **configuration file preview**. Keep it — it contains the three OCIDs
you would otherwise have to hunt for. Check the fingerprint it displays matches:

```bash
openssl rsa -pubout -outform DER -in ~/.oci/oci_api_key.pem 2>/dev/null | openssl md5 -c
```

## 4. Write `~/.oci/config`

Paste Oracle's preview into `~/.oci/config`, then correct the `key_file` line — the preview
leaves it as a placeholder:

```ini
[DEFAULT]
user=ocid1.user.oc1..xxxxx
fingerprint=xx:xx:xx:...
tenancy=ocid1.tenancy.oc1..xxxxx
region=us-ashburn-1
key_file=~/.oci/oci_api_key.pem
```

```bash
chmod 600 ~/.oci/config
oci iam region list --output table   # proves the credentials work
```

## 5. Run it

```bash
./infra/oracle/launch-retry.sh --once    # confirm discovery works, one pass
./infra/oracle/launch-retry.sh           # then leave it running
```

It prints the public IP and the ssh command when capacity is granted, and stops. An auth,
parameter or quota error stops it immediately with the message — retrying a real
misconfiguration would just bury it. Endpoint timeouts and throttling are retried instead,
because one AD blipping mid-sweep should not end an overnight wait, and an unrecognised
failure is tolerated six times in a row before it gives up.

Tunables: `SHAPE_LADDER` (default `4:24 2:12 1:6`), `BOOT_GB` (150), `INTERVAL` (300s),
`DISPLAY_NAME`, `SSH_KEY_FILE`. `OCPUS` and `MEMORY_GB` are **not** settings — they are
read off the ladder on each attempt, and exporting them does nothing.

**Asking for less gets you in sooner, and the ladder asks for everything.** The Always Free
allocation is 4 OCPU / 24 GB of A1, but a request for all of it competes for a scarcer slot
than a request for half — so each pass tries 4:24, then 2:12, then 1:6 in every
availability domain, and takes whichever lands first. The stack's compose memory limits
total about 5 GB, so even the bottom rung is enough to run on, and an instance that landed
small can be resized later. Set `SHAPE_LADDER` to a single rung to insist on one size.
