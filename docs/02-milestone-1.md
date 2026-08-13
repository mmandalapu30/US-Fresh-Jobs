# Milestone 1 — Foundation

**Status: COMPLETE and verified.** 118 tests passing, migration applied and rolled back
against PostgreSQL 17.9, lint and format clean.

Scope: repository structure, Docker development environment, PostgreSQL, database
migrations, shared packages, the API operational surface, and CI. No source-specific code
— that begins in Milestone 3, on top of the connector interface.

---

## 1. Files created

### Configuration and tooling
| File | Purpose |
|---|---|
| `pyproject.toml` | Workspace ruff / mypy / pytest / coverage config |
| `Makefile` | 22 developer targets (`make help`) |
| `docker-compose.yml` | Postgres 17, Redis 7, MinIO, API, worker, beat |
| `.gitignore` | Blocks every `.env.*` except the two templates |
| `.env.example` | Every variable, all secrets as `CHANGE_ME` |
| `.env.development` | Generated dev secrets (git-ignored) |
| `.env.test` | Isolated test DB, in-memory Celery, rate limiting off |
| `.env.production.example` | Production template, placeholders only |
| `.github/workflows/ci.yml` | lint, secret scan, tests, docker build, source drift |

### Shared packages
| File | Purpose |
|---|---|
| `packages/shared/jobplatform_shared/config.py` | Typed settings + production hardening gate |
| `packages/shared/jobplatform_shared/logging.py` | Structured JSON logging with secret redaction |
| `packages/shared/jobplatform_shared/db.py` | Separate async (API) and sync (worker) engines |
| `packages/shared/jobplatform_shared/time.py` | UTC-only helpers; `is_future` with tolerance |
| `packages/shared/jobplatform_shared/errors.py` | Exception hierarchy with stable codes |
| `packages/schemas/jobplatform_schemas/enums.py` | 12 domain enums incl. 14 rejection reasons |
| `packages/schemas/jobplatform_schemas/us_states.py` | 50 states + DC + territories, homograph list |
| `packages/schemas/jobplatform_schemas/pagination.py` | Cursor (keyset) pagination primitives |

### Database
| File | Purpose |
|---|---|
| `database/migrations/alembic.ini` | `script_location = %(here)s` — runs from any cwd |
| `database/migrations/env.py` | DSN from environment; autogenerate deliberately off |
| `database/migrations/versions/0001_initial_schema.py` | 20 tables, 11 enums, 32 partitions |

### API
| File | Purpose |
|---|---|
| `apps/api/app/main.py` | App factory, request-id, security headers, access log |
| `apps/api/app/core/errors.py` | Uniform error envelope; no internals leaked |
| `apps/api/app/core/metrics.py` | Prometheus registry incl. pre-declared ingestion metrics |
| `apps/api/app/routers/health.py` | `/health`, `/ready`, `/metrics` |
| `apps/api/app/routers/meta.py` | `/api/v1/locations/states`, `/api/v1/meta/enums` |

### Infrastructure, tests, docs
`infra/docker/api.Dockerfile`, `infra/docker/worker.Dockerfile`,
`infra/docker/postgres/init.sql`, `conftest.py`, `tests/test_config.py`,
`tests/test_us_states.py`, `tests/test_migrations.py`, `apps/api/tests/test_health.py`,
`scripts/verify_source.py`, `docs/00-source-verification.md`, `docs/01-architecture.md`.

---

## 2. Database migration

`0001_initial_schema` creates:

- **20 tables** — every table the spec listed, plus `company_sources`, `sync_files`
  (per-file checkpoints), `alert_deliveries` (outbox), `job_category_map`, `audit_log`.
- **11 native enums**, **32 monthly partitions** across `job_events` and `job_snapshots`,
  plus a `DEFAULT` partition on each so an out-of-range insert never fails.
- **15 indexes on `jobs`**, four of them partial (`WHERE status='ACTIVE'`), which roughly
  halves their size given the verified ~50% closed ratio.
- **6 CHECK constraints on `jobs`** and the uniqueness rules that make dedupe and
  idempotency database-enforced rather than application-hoped.

Run:

```bash
make migrate                  # apply
make migrate-sql              # print reviewable SQL without applying (557 lines)
make migrate-down             # roll back one revision
```

---

## 3. Environment variables

Full list in `.env.example`. The ones with non-obvious, evidence-driven defaults:

| Variable | Default | Why |
|---|---|---|
| `OPENJOBDATA_VARIANT` | `full` | `minimal` has no city/state/salary/description (source doc §5) |
| `OPENJOBDATA_INCLUDE_ENTIRE_JSON` | `false` | Excluding it halves the daily read: 238 MB → 120 MB |
| `INGEST_MAX_FUTURE_POSTED_AT_HOURS` | `24` | Source publishes `posted_at` up to 34 days in the future |
| `INGEST_MIN_POSTED_AT_YEAR` | `2000` | Source has `posted_at` back to 2013; older is implausible |
| `LIFECYCLE_REMOVED_AFTER_DAYS` | `14` | Publication gaps of 1–2 days must not mass-mark jobs REMOVED |
| `OPENJOBDATA_MAX_CONCURRENCY` | `2` | Rate limits are **unknown** (source doc §10) — conservative until measured |

---

## 4. Tests

```
118 passed
├─ tests/test_us_states.py      57   location/country resolution, homographs
├─ tests/test_migrations.py     22   real PostgreSQL, marked `integration`
├─ tests/test_config.py         21   settings + production hardening gate
└─ apps/api/tests/test_health.py 18   health/ready/metrics/errors/security headers
```

```bash
make test                 # everything
make test-unit            # 96 tests, no database needed
pytest -m integration     # 22 tests against real PostgreSQL
```

Integration tests create a uniquely-named database, migrate it, and drop it afterwards.
They skip cleanly when no server is reachable, so `make test-unit` works anywhere.

**There is no SQLite fallback.** The schema depends on native enums, partitioning, partial
indexes, `citext` and `tsvector`; a SQLite "equivalent" would be testing a different product.

---

## 5. How to run locally

```bash
make init            # create .env.development from the template
make up              # postgres + redis + minio + api
make migrate         # apply the schema
curl localhost:8000/health
curl localhost:8000/ready
open http://localhost:8000/docs
```

Without Docker (what was used to verify this milestone), point `DATABASE_URL` at a local
PostgreSQL 17 and run `make migrate && make test`.

---

## 6. Expected output

```
$ make migrate
INFO  [alembic.runtime.migration] Running upgrade  -> 0001, Initial schema: ...

$ curl -s localhost:8000/health
{"status":"ok","service":"api","version":"0.1.0","environment":"development",
 "uptime_seconds":12.34,"timestamp":"2026-08-10T06:15:00.000000+00:00"}

$ curl -s localhost:8000/ready
{"status":"ready","dependencies":[
  {"name":"postgres","healthy":true,"latency_ms":1.82,"error":null},
  {"name":"redis","healthy":true,"latency_ms":0.71,"error":null}],
 "timestamp":"..."}

$ python -m pytest -q
118 passed
```

---

## 7. Acceptance criteria — verified

| # | Criterion | Evidence |
|---|---|---|
| 1 | Migration applies to PostgreSQL 17 | `alembic upgrade head` → 53 relations, 11 enums |
| 2 | Migration is reversible | `downgrade base` → 1 table left (`alembic_version`), 0 enums; re-upgrade clean |
| 3 | Migration produces reviewable SQL | `upgrade head --sql` → 557 lines |
| 4 | `posted_at` nullable, `first_seen_at` not | asserted in `test_migrations.py` |
| 5 | Seven distinct timestamps, all `timestamptz` | asserted |
| 6 | Dedupe L1 enforced by the database | `job_sources_uq` raises `UniqueViolation` |
| 7 | Same job from two sources allowed | 2 provenance rows on one job |
| 8 | Bad data rejected with named constraints | 5 CHECK violations asserted by name |
| 9 | Search vector auto-maintained on insert **and** update | asserted both ways |
| 10 | Partitions route by month; out-of-range → DEFAULT, not error | asserted |
| 11 | One active sync run per source | `sync_runs_one_active_per_source` |
| 12 | No duplicate alert per (alert, job) | `alert_deliveries_uq` |
| 13 | Email uniqueness case-insensitive | `citext`; `A@Example.com` collides with `a@example.com` |
| 14 | Production refuses unsafe config | 8 hardening tests |
| 15 | Secrets never in `repr`, logs, or `/ready` | redaction tests + credential-leak test |
| 16 | `/health` stays green when dependencies are down | asserted |
| 17 | `/ready` returns 503 when they are | asserted |
| 18 | Ingestion metrics pre-declared | asserted on `/metrics` |
| 19 | Lint and format clean | `ruff check` + `ruff format --check` pass |
| 20 | No secret in any tracked file | `.gitignore` + CI secret scan + template check |

---

## 8. Deliberately NOT in Milestone 1

Called out so nothing looks accidentally missing:

- No `SourceConnector` implementation. The interface lands in M2, `OpenJobDataConnector`
  in M3. Writing connector code before the interface is what produces the
  source-logic-everywhere problem the spec forbids.
- No job endpoints. `/api/v1/jobs` needs the repository and search layers (M9–M10).
- No `apps/web`. The Next.js app is M11; scaffolding it now would rot.
- No Celery tasks. `celery_app.py` arrives with the first real task in M3; the worker
  service is already defined in compose behind a `workers` profile.
- `mypy` is advisory in CI until M9, when the typed API layer exists. Enforcing strict mode
  on a skeleton produces noise, not safety.

---

## 9. Verified environment

| Component | Version | How it was verified |
|---|---|---|
| PostgreSQL | 17.9 | migration applied, rolled back, re-applied; 22 integration tests |
| Python | 3.12.10 | full suite |
| Alembic / SQLAlchemy | 1.19.1 / 2.0.51 | upgrade, downgrade, offline SQL |
| Pydantic / pydantic-settings | 2.13.4 / 2.15.0 | settings + hardening tests |
| Docker | **not installed on this machine** | compose file and Dockerfiles are authored but **unbuilt** — CI's `docker-build` job is what will first prove them |

That last row is the one open item in this milestone: the container images are written but
have not been built anywhere yet.

---

## 10. Next — Milestone 2

Object storage client, the `SourceConnector` Protocol, `sync_runs`/`sync_files` repository,
and the CI layering check (`scripts/check_layering.py`) that fails the build if
`openjobdata`, `huggingface` or `hf://` appears outside the connector package.
