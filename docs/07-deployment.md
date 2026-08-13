# 07 — Deploying to a Linux server

Target: a single Linux VPS running Docker, with the daily ingest driven by systemd
timers. Everything below is in the repo; nothing needs to be written on the server.

> **Verification status, stated plainly.** The compose files and Dockerfiles here were
> written and statically validated (`docker compose config` resolves both stacks, all
> services and profiles included), and the web image's build was proven to work with no
> API reachable — the failure mode that matters for `docker build`. They have **not** been
> built or run, because the Docker engine on the development machine does not start (the
> WSL2 backend is not enabled; see `docs/05-milestones-4-to-11.md` §7). Treat the first
> `up` as the real test and work through §6 if a service does not come up.

---

## 1. What runs where

| Container | Public? | Notes |
|---|---|---|
| `caddy` | **yes**, :80/:443 | The only published port. Terminates TLS, proxies to `web`. |
| `web` | no | Next.js standalone server on :3100, internal only. |
| `api` | no | FastAPI on :8000, internal only. |
| `postgres` | no | No host port. |
| `redis` | no | No host port. |
| `minio` | no | Raw source archive, S3-compatible. No host port; console not reachable from outside. |
| `minio-init` | n/a | One-shot bucket creation on every `up`. Idempotent. |
| `ingest` | n/a | One-shot, `profiles: ["ingest"]`. Started by the timers, never by `up`. |

The API is deliberately unreachable from outside. The browser talks only to Next, and Next
talks to the API inside the compose network — which is what makes the platform's rule that
the frontend never holds credentials or learns the API host structural rather than a
convention. Do not add an `api` port mapping or a `/api` route in the Caddyfile.

---

## 2. First deploy

```bash
# on the server, as a user in the docker group
git clone <your-remote> /srv/job-platform
cd /srv/job-platform

cp .env.production.example .env.production
# Then edit it. Every CHANGE_ME must be replaced. In particular:
#   SITE_ADDRESS               your domain, e.g. jobs.example.com (:80 to test without DNS)
#   POSTGRES_PASSWORD          generate: openssl rand -base64 36
#   SECRET_KEY / JWT_SECRET    generate: python -c "import secrets;print(secrets.token_urlsafe(64))"
#   CORS_ALLOW_ORIGINS         https://your-domain
#   OBJECT_STORAGE_ACCESS_KEY  MinIO root user,     generate: openssl rand -hex 24
#   OBJECT_STORAGE_SECRET_KEY  MinIO root password, generate: openssl rand -hex 24
#   INGEST_CATEGORY_ALLOWLIST  data-engineering,software
```

**Two rules the hardening check enforces, both of which stop the API booting.** It runs
only when `ENVIRONMENT=production`, so neither shows up in development:

- **All four secrets must be at least 32 characters** — `SECRET_KEY`, `JWT_SECRET` and
  *both* object-storage credentials, the access key included. MinIO's own minimums are
  much lower, so a key MinIO accepts can still stop the API.
- **`CORS_ALLOW_ORIGINS` must be https**, and must not be `*`. This applies even when you
  are testing on `SITE_ADDRESS=:80` over plain HTTP — set `https://<host>` regardless.
  Nothing breaks, because the browser never calls the API cross-origin; Next proxies it
  server-side, so the value is validated but effectively unused by the UI.

`INGEST_CATEGORY_ALLOWLIST` deserves the attention. **Empty means every category**, which
is the shipped default and correct as a default — but leaving it unset on this deployment
once pulled in 63,760 out-of-scope rows that then had to be purged. Set it before the
first ingest, not after.

```bash
docker compose -f infra/docker/docker-compose.prod.yml --env-file .env.production \
  up -d --build

# schema (first deploy only, and after any migration)
docker compose -f infra/docker/docker-compose.prod.yml --env-file .env.production \
  run --rm --no-deps ingest \
  python -m alembic -c database/migrations/alembic.ini upgrade head
```

Then load the first data — this takes a while and is worth watching rather than
backgrounding:

```bash
COMPOSE=1 ./scripts/daily.sh
```

Finally install the schedule, so tomorrow's file arrives without anyone typing anything:

```bash
sudo ./scripts/install-systemd.sh
```

---

## 3. The daily schedule

Two systemd timers, installed by one command:

```bash
sudo ./scripts/install-systemd.sh
```

| Timer | When (America/New_York) | Does |
|---|---|---|
| `jobplatform-daily` | 09:00 daily | ingest, then retention |
| `jobplatform-catchup` | 11:00, 13:00, 15:00, 17:00 | ingest **only if today's file is still missing** |

**Why 09:00 needs the catch-ups.** The source publishes each day's file somewhere between
06:33 and 14:15 UTC and the hour genuinely varies (`docs/00-source-verification.md` §5).
09:00 Eastern is 13:00 UTC in summer and 14:00 UTC in winter — inside that window, not
after it. So the primary run lands before the file exists on a fair share of days. On its
own that is not data loss, because `discover()` diffs the remote listing against our
checkpoints and the next run collects whatever was missed — but "the next run" is
tomorrow, and the board would show yesterday's jobs all day.

The afternoon passes close that to hours. They are close to free: each one asks
`scripts/have_todays_file.py` a single indexed question, and once the day's file is in it
stops there — no directory listing, no download, no `sync_runs` row.

```
09:00  primary   ingest + retention
11:00  catch-up  today's file already in?  yes -> exit, no -> ingest
13:00  catch-up  ...
15:00  catch-up  ...
17:00  catch-up  ...
```

The timezone is named in `OnCalendar=`, not converted to a fixed offset, so 09:00 stays
09:00 through both daylight-saving transitions. That needs systemd 240 or newer (Ubuntu
20.04+, Debian 11+); the installer asks the host's own `systemd-analyze` to parse every
expression before it writes anything, and stops with the reason if the host is too old.

**Checking on it:**

```bash
systemctl list-timers 'jobplatform-*'          # next and last elapse
journalctl -u jobplatform-daily.service -f     # the primary run, live
journalctl -u jobplatform-catchup.service --since today
systemctl start jobplatform-daily.service      # run now, out of schedule
./scripts/install-systemd.sh --dry-run         # what would be written, no root needed
sudo ./scripts/install-systemd.sh --remove     # uninstall
```

A catch-up that found nothing to do reads:

```
=== catch-up run starting ===
2026-08-13: already ingested (990 rows, at 13:16 UTC)
today's file is already in - nothing to do
=== catch-up run finished (skipped) ===
```

### Cron instead

If you would rather not use systemd — note that `CRON_TZ` is a Vixie-cron/cronie
extension and is not available in every cron:

```cron
CRON_TZ=America/New_York
0 9      * * *  cd /srv/job-platform && COMPOSE=1 ./scripts/daily.sh            >> /var/log/jobplatform-daily.log 2>&1
0 11-17/2 * * *  cd /srv/job-platform && COMPOSE=1 ./scripts/daily.sh --catch-up >> /var/log/jobplatform-daily.log 2>&1
```

Without `CRON_TZ`, express the slots in UTC — `13:00` and `15,17,19,21:00` match Eastern
during daylight saving and drift an hour when the US returns to standard time.

### What the schedule relies on

Four behaviours, all of them tested:

- **Transient source failures are retried**, three attempts with a 60s/120s backoff. The
  source resets connections and times out reads often enough to end a run on its own
  (roughly 1 request in 8 from one observed host). Retrying resumes rather than redoes:
  `sync_files` checkpoints each completed file.
- **Overlapping runs are refused, not duplicated.** If a slow primary run is still going
  when the 11:00 catch-up fires, the catch-up is refused the per-source lock, logs
  `another ingest is already running` and exits 0. The timers are deliberately not
  `Conflicts=` with each other — the database lock is the better arbiter, because it
  refuses the newcomer instead of killing a run mid-write.
- **An unknown answer never skips a day.** If `have_todays_file.py` cannot reach the
  database it exits 2, and the catch-up ingests anyway. Failing closed there would turn a
  transient database blip into a silently skipped day.
- **Retention being switched off is not a failure.** With `RETENTION_MAX_POSTED_AGE_DAYS=0`
  (the default) the step exits 2 and is skipped, rather than failing the run after a
  perfectly good ingest. Retention runs only in the 09:00 pass; repeating it four times
  could not remove anything the first pass had not.

`/admin` shows the same run history in the browser.

---

## 4. Updating

```bash
cd /srv/job-platform
git pull
docker compose -f infra/docker/docker-compose.prod.yml --env-file .env.production \
  up -d --build
# only if the release adds migrations
docker compose -f infra/docker/docker-compose.prod.yml --env-file .env.production \
  run --rm --no-deps ingest \
  python -m alembic -c database/migrations/alembic.ini upgrade head
```

Containers run the code baked into the image — there are no source bind-mounts in
production — so a deploy is a rebuild, never an edit in place.

**Never round-trip a migration against the production database to check it reverses.**
Doing exactly that on a populated database once dropped two columns and wiped 182,766
classifications (`docs/06-role-filtering.md` §6.5). Verify reversibility on a throwaway
database.

---

## 5. Backups

Not configured by this stack — decide before you have data worth losing.

```bash
# nightly dump, keeping 14 days
0 4 * * * docker exec jobplatform-postgres pg_dump -U <user> -d <db> --format=custom \
  > /var/backups/jobplatform-$(date +\%F).dump && \
  find /var/backups -name 'jobplatform-*.dump' -mtime +14 -delete
```

A dump is not a backup until a restore has been tested. Restore into a scratch database
and count rows before trusting it.

**The raw archive grows without bound.** MinIO holds one ~120 MB source file per day
with versioning on, so the `minio_data` volume gains roughly **44 GB a year** and
nothing prunes it. Postgres is the system of record — the archive exists to re-run an
ingest against a past day — so the cheap answer is a bucket lifecycle rule rather than a
bigger disk:

```bash
# keep 90 days of raw files. Sets the alias first: the one `minio-init` created lives in
# that container, not this one.
docker exec jobplatform-minio sh -c   'mc alias set me http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" &&
   mc ilm rule add --expire-days 90 me/jobplatform-raw'

# older mc builds spell it: mc ilm add --expiry-days 90 me/jobplatform-raw
```

Confirm with `mc ilm rule ls me/jobplatform-raw`, and watch the volume with
`docker system df -v | grep minio_data`.

---

## 6. If something does not come up

| Symptom | Cause | Fix |
|---|---|---|
| `web` build fails fetching the API | A page reverted to build-time prerendering | Every page that fetches must export `dynamic = "force-dynamic"`. `docker build` has no API to reach. |
| Web serves but every page 500s | `API_BASE_URL` wrong | It is set in compose to `http://api:8000/api/v1`. Check `docker compose logs api`. |
| Caddy will not get a certificate | DNS not pointing here, or :80 blocked | Certificate issuance needs inbound :80. Test with `SITE_ADDRESS=:80` first. |
| Ingest exits 3 every time | A run is wedged `RUNNING` | Wait 120 minutes for the automatic reclaim, or mark it `FAILED` by hand. |
| Ingest stores far more than expected | `INGEST_CATEGORY_ALLOWLIST` empty | Set it, then `python scripts/purge_out_of_scope.py` (dry run first). |
| Ingest fails `NoSuchBucket` or connection refused to `minio:9000` | `up` was run before the storage credentials were set, so `minio-init` never created the bucket | `docker compose ... up -d minio minio-init` and check `docker logs jobplatform-minio-init` |
| `minio` will not start | Access key under 3 chars or secret under 8 | MinIO enforces both minimums; regenerate with `openssl rand -base64 24` |
| `postgres` restarts on first boot | `POSTGRES_INITDB_ARGS` changed after init | Data checksums are settable only at initdb. Changing later needs dump/restore. |

---

## 7. Not included

Stated so nobody assumes otherwise:

- **No monitoring or alerting.** A failed timer writes to the journal, and nothing reads
  the journal. `systemctl list-timers 'jobplatform-*'` and `/admin` are the only places a
  silent failure shows up, and both need a human to look. Milestone 15.
- **No authentication.** `/admin` is public and exposes aggregates only. Milestone 12.
- **Description HTML is not sanitised** — required before rendering source HTML.
- **Single host, no redundancy.** Postgres, Redis and the app share one machine.
