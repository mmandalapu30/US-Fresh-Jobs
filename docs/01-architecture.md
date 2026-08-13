# 01 — Architecture

Read [`00-source-verification.md`](00-source-verification.md) first. This document assumes
its measured facts.

---

## A. Final architecture

### A.1 Component view

```
                      ┌──────────────────────────────────────────┐
   Users ───HTTPS───► │  Next.js 15 (App Router, TS, Tailwind)   │
                      │  SSR/ISR feed · client-side filters      │
                      └───────────────┬──────────────────────────┘
                                      │ REST /api/v1  (cursor-paginated)
                      ┌───────────────▼──────────────────────────┐
                      │  FastAPI (uvicorn, Pydantic v2)          │
                      │  routers → services → repositories       │
                      │  JWT auth · Redis rate-limit · OTel       │
                      └───┬──────────────────┬───────────────────┘
                          │                  │
              ┌───────────▼──────┐   ┌───────▼────────┐
              │  PostgreSQL 17   │   │   Redis 7      │
              │  jobs + FTS      │   │ cache · limits │
              │  partitioned     │   │ Celery broker  │
              │  events/snapshots│   └───────┬────────┘
              └───────────▲──────┘           │
                          │                  │
              ┌───────────┴──────────────────▼───────────────────┐
              │  Celery workers  (ingestion / lifecycle / alerts)│
              │  Celery Beat scheduler                           │
              └───────────▲──────────────────────────────────────┘
                          │
              ┌───────────┴──────────┐        ┌──────────────────┐
              │  Source Connectors   │◄───────┤ S3 / MinIO       │
              │  OpenJobDataConnector│        │ raw · snapshots  │
              │  (future: Greenhouse,│        │ rejects · manifest│
              │   Ashby, Lever, ...) │        └──────────────────┘
              └───────────▲──────────┘
                          │ hf:// via HfFileSystem + PyArrow range reads
              ┌───────────┴──────────┐
              │ HF Storage Bucket    │
              │ OpenJobData (MIT)    │
              └──────────────────────┘
```

### A.2 Layering rule (the thing that keeps sources swappable)

```
router  →  service  →  repository  →  SQLAlchemy Core/ORM  →  Postgres
                    ↘  search_service (Protocol)  ↘ PostgresSearchService | OpenSearchSearchService
worker  →  pipeline stages  →  connector (Protocol)  →  OpenJobDataConnector | GreenhouseConnector
```

**Hard rule enforced by CI (`scripts/check_layering.py`, added M5):** the string
`openjobdata`, `huggingface`, or `hf://` may appear **only** under
`workers/ingestion/ingestion/connectors/openjobdata/` and its tests. Any other match fails the
build. This is how "do not hard-code OpenJobData logic throughout the application" becomes
mechanically true instead of aspirational.

### A.3 Why these choices

| Decision | Reason |
|---|---|
| `full` variant, projection excluding `entire_json` | Only source of city/state/salary/description. Halves transfer (§5 of source doc). |
| Celery + Redis, not Kafka/K8s | Spec demands cheap first deploy. One `docker compose up`. Revisit at metric-proven need. |
| Postgres FTS first | `tsvector` + GIN handles millions of rows well. `SearchService` Protocol keeps OpenSearch a drop-in. |
| Cursor pagination | `OFFSET 500000` is a table scan. Keyset on `(sort_key, id)` is O(log n) forever. |
| Native enums | Compact, self-documenting; `ALTER TYPE ADD VALUE` covers evolution. |
| Partition `job_events`/`job_snapshots` by month | Append-only, highest growth, always queried by time window. |
| `jobs` **not** partitioned in v1 | Partition key would have to join every unique constraint, breaking dedupe upserts. Revisit at ~50M rows via `pg_partman` on `posted_at`. Documented, not deferred silently. |

---

## B. Repository structure

```
job-platform/                       ← repo root (this directory)
├── apps/
│   ├── api/                        FastAPI service
│   │   ├── app/
│   │   │   ├── main.py             app factory, middleware, lifespan
│   │   │   ├── core/               settings glue, security, rate-limit, errors
│   │   │   ├── routers/            v1 endpoints (thin — no business logic)
│   │   │   ├── services/           business logic  (M11+)
│   │   │   ├── repositories/       SQL access      (M11+)
│   │   │   └── search/             SearchService Protocol + impls (M12)
│   │   └── tests/
│   └── web/                        Next.js app (M13+)
├── workers/
│   └── ingestion/
│       └── ingestion/
│           ├── celery_app.py
│           ├── connectors/         SourceConnector Protocol
│           │   └── openjobdata/    ← ONLY place OpenJobData knowledge lives
│           ├── pipeline/           validate → normalize → classify → dedupe → load
│           ├── services/           LocationNormalizer, FreshnessService, DataQuality
│           └── tasks/              Celery tasks
├── packages/
│   ├── shared/                     config, structured logging, db, redis, time, errors
│   └── schemas/                    Pydantic contracts + enums + US reference data
├── database/
│   └── migrations/                 Alembic (versioned, never autogenerate in prod)
├── infra/
│   ├── docker/                     Dockerfiles + postgres init
│   └── terraform/                  cloud IaC (M20)
├── docs/
├── scripts/
├── tests/                          cross-cutting integration tests
└── .github/workflows/
```

Both `packages/*` are installed editable into the API and worker images, so the contract types
are literally the same objects on both sides of the wire.

---

## C. Database schema

### C.1 ERD

```
users ──1:N── saved_jobs ──N:1── jobs
  │                                 │
  ├──1:N── saved_searches           ├──N:1── companies ──1:N── company_sources
  │            │                    │
  │            └─1:N─ user_alerts   ├──N:1── job_locations
  │                      │          │
  │                      └─1:N─ alert_deliveries (outbox)
  │                                 ├──1:N── job_sources     (provenance, N sources per job)
  └──1:N── audit_log                ├──1:N── job_events      (PARTITIONED BY MONTH)
                                    ├──1:N── job_snapshots   (PARTITIONED BY MONTH)
                                    ├──M:N── skills          via job_skills
                                    └──M:N── categories      via job_categories

sync_runs ──1:N── sync_errors
sync_runs ──1:N── sync_files        (per-file checkpoint → resumability)
```

### C.2 The timestamp model (non-negotiable)

Seven distinct columns. Collapsing any two of them loses information the source actually
provides:

| Column | Source | Meaning |
|---|---|---|
| `posted_at` | `job_model_json.posted_at` / `posted_at` | employer posted it. **Nullable (19%)**, may be future |
| `posted_at_is_valid` | derived | false when future-dated or absurdly old → excluded from "fresh" |
| `first_seen_at` | **our clock**, on INSERT | first time *this platform* saw it. Never overwritten |
| `last_seen_at` | **our clock**, every observation | drives REMOVED detection |
| `last_updated_at` | our clock, only when `content_hash` changes | real content change, not re-observation |
| `source_fetched_at` | `fetched_time` | when the *upstream* pipeline fetched it |
| `close_at` | `job_model_json.expires_at` | employer's stated expiry (10% coverage) |
| `closed_at` | `close_time` | when *source* detected closure |

`first_seen_at` never replaces `posted_at`. The job detail page shows both, labelled, exactly
as the spec requires.

### C.3 Indexing strategy

Ingestion-cost-aware — every index slows the upsert path, so each one below earns its place:

```sql
-- feed: "fresh US jobs", the hottest query in the product
CREATE INDEX jobs_feed_idx ON jobs (country_code, status, posted_at DESC, id DESC)
  WHERE status = 'ACTIVE' AND posted_at_is_valid;

-- state pages
CREATE INDEX jobs_state_idx ON jobs (country_code, state_code, posted_at DESC, id DESC)
  WHERE status = 'ACTIVE';

-- remote filter
CREATE INDEX jobs_remote_idx ON jobs (country_code, remote_type, posted_at DESC, id DESC)
  WHERE status = 'ACTIVE';

-- detection feed ("new to our platform today")
CREATE INDEX jobs_first_seen_idx ON jobs (first_seen_at DESC, id DESC);

-- full-text
CREATE INDEX jobs_fts_idx ON jobs USING GIN (search_vector);

-- trigram company/title autocomplete
CREATE INDEX companies_name_trgm ON companies USING GIN (name_normalized gin_trgm_ops);

-- dedupe probes (unique = also the correctness constraint)
CREATE UNIQUE INDEX job_sources_uq  ON job_sources (source, external_job_id);
CREATE UNIQUE INDEX jobs_content_uq ON jobs (canonical_job_id);
CREATE INDEX jobs_apply_url_hash_idx ON jobs (apply_url_hash) WHERE apply_url_hash IS NOT NULL;
CREATE INDEX jobs_fingerprint_idx    ON jobs (dedupe_fingerprint);
```

Partial indexes (`WHERE status='ACTIVE'`) matter: verified data is ~50% closed, so the active
index is roughly half the size and stays hot in cache.

---

## D. Data ingestion flow

```
 (1) DISCOVER      list hf://.../data/full/changes/  → [(date, path, size, mtime)]
                   diff against sync_files watermarks (handles the VERIFIED skipped days)
        │
 (2) ARCHIVE       stream file → S3/MinIO raw/openjobdata/full/changes/YYYY-MM-DD.parquet
                   (immutable audit copy; re-runs read from here, not the network)
        │
 (3) READ          PyArrow, row-group at a time, columns projected
                   EXCLUDING entire_json  →  ~120 MB not 238 MB
        │
 (4) VALIDATE      Pydantic per row. Failures → sync_errors WITH reason. Never silently dropped.
        │
 (5) NORMALIZE     double-decode job_model_json → location/compensation/description
                   strip base64 data: URIs, sanitize HTML
        │
 (6) LOCATION      LocationNormalizer → country_code, state_code, city, postal, remote
                   rejects Canadian/Mexican homographs (London ON ≠ London KY)
        │
 (7) FRESHNESS     posted_at validity gate → freshness bucket
        │
 (8) DEDUPE        L1 (source,external_job_id) → L2 canonical apply URL
                   → L3 company+title+location → L4 content fingerprint
        │
 (9) LIFECYCLE     ACTIVE / EXPIRED / REMOVED / UNKNOWN + job_events
        │
(10) LOAD          COPY to UNLOGGED staging → single MERGE/upsert txn → job_events
                   checkpoint sync_files row  ← resumable here
        │
(11) INDEX         search_vector maintained by generated column (no separate step in v1)
```

**Idempotency contract.** Steps 4–10 are pure functions of (file bytes, DB state). Re-running a
file is a no-op except for `last_seen_at`. Guaranteed by:

- `job_sources (source, external_job_id)` UNIQUE → the same source row can only ever map to one job.
- Per-file `sync_files` checkpoint with `(sync_run_id, remote_path)` UNIQUE and a `rows_committed` offset.
- The upsert transaction commits the staging merge **and** the checkpoint together.
  A crash mid-file rolls back both — the next run re-reads that file cleanly.

---

## E. Security model

| Layer | Control |
|---|---|
| Transport | TLS terminated at Caddy/ALB; HSTS; HTTP→HTTPS redirect |
| AuthN | Argon2id password hash; JWT access (15 min) + rotating refresh (30 d) in `HttpOnly`, `Secure`, `SameSite=Lax` cookies |
| CSRF | Cookie auth ⇒ double-submit token on all unsafe methods |
| AuthZ | Role on token (`anon`/`user`/`admin`/`worker`); every owned resource re-checks `user_id` in the **repository** layer, not the router |
| Input | Pydantic v2 strict at the boundary; all SQL via bound parameters (zero string-built SQL) |
| Rate limit | Redis sliding window: anon 60/min · user 300/min · admin 1000/min · worker exempt-by-mTLS-or-internal-network |
| Abuse | Per-IP + per-token buckets, `Retry-After`, progressive backoff on auth failures |
| Secrets | Env-injected only. `.env.*` git-ignored; `.env.example` holds names + dummy values. No secret literal in any tracked file |
| DB creds | Never leave the API/worker containers. The browser talks only to `/api/v1` |
| Audit | `audit_log` for auth events, admin actions, alert changes |
| Headers | CSP, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options: DENY` |
| CORS | Explicit origin allowlist from settings; `*` rejected at startup when `ENVIRONMENT=production` |

**Untrusted content:** job descriptions are third-party HTML. Sanitized server-side at
ingestion (allowlist tags/attrs, `data:`/`javascript:` URIs stripped) *and* rendered through a
sanitizer client-side. Stored HTML is never injected raw.

---

## F. Deployment architecture

**Stage 1 — single host (launch, ~$25–50/mo).** One `docker compose` file, Caddy for
automatic TLS, Postgres + Redis + MinIO as containers with named volumes, nightly `pg_dump`
to off-host object storage. Deliberately no Kubernetes, no managed search, no service mesh.

**Stage 2 — managed data (when metrics demand).** Postgres → RDS/Cloud SQL with a read
replica; Redis → ElastiCache; MinIO → S3. API and workers scale horizontally behind an ALB.
Search reads route to the replica so ingestion write load stops competing with user search —
this is the "separate ingestion from search workloads" requirement, and it is a config change
because all reads already go through the repository layer.

**Stage 3 — only if proven.** OpenSearch behind the existing `SearchService` Protocol;
`jobs` partitioning via `pg_partman`; container orchestration.

Promotion triggers are numeric, not vibes: p95 search > 300 ms sustained 24 h → Stage 2 search
replica. Ingestion lag > 6 h → more workers. DB CPU > 70 % sustained → Stage 2 fully.

---

## G. Development milestones

| # | Milestone | Exit criterion |
|---|---|---|
| **1** | **Foundation** — monorepo, Docker dev env, Postgres, full migration set, shared packages, health API, CI | `docker compose up` → migrations apply → `/health` + `/ready` green → tests pass |
| 2 | Object storage + `SourceConnector` Protocol + `sync_runs` tracking | Protocol + MinIO round-trip tested; no source code yet |
| 3 | `OpenJobDataConnector`: `discover()` / `fetch()` with checkpointing | Lists real bucket, detects the 3 skipped days, archives to MinIO |
| 4 | Parquet pipeline: projected read, double-decode, validation, rejects | 81k-row file parsed; rejects carry reasons |
| 5 | `LocationNormalizer` + US classification | 50 states + DC; CA/MX homographs rejected; ≥99% US recall on real file |
| 6 | Dedup L1–L4 + fingerprint | Cross-source collision suite green |
| 7 | Lifecycle + `job_events` + `FreshnessService` | Future/NULL `posted_at` handled per §7 |
| 8 | Full ingestion task + idempotency + crash recovery | Same file twice = 0 dupes; kill -9 mid-run resumes clean |
| 9 | FastAPI v1 read endpoints + cursor pagination + Redis cache | p95 < 300 ms on ≥1M rows |
| 10 | Postgres search + `SearchService` Protocol | All spec filters; contract test suite reusable for OpenSearch |
| 11 | Next.js feed + job detail | Both timestamps shown, labelled, never fabricated |
| 12 | Auth + saved jobs + saved searches | Full security test suite |
| 13 | Alerts + notification outbox | No duplicate alert for the same job, proven under retry |
| 14 | Admin dashboard + ingestion observability | Every metric in the spec rendered |
| 15 | Monitoring, alerting, runbooks | Volume-drop and duplicate-spike alerts fire in staging |
| 16 | Production deployment | Stage 1 live, backups verified by restore drill |

---

## H. Acceptance criteria (product-level)

1. Ingestion applies **no row cap**. A 62k-US-row day lands 62k rows.
2. `posted_at` is never overwritten by `first_seen_at`; both are visible in the UI.
3. Future-dated `posted_at` never appears in a "posted recently" bucket.
4. Reprocessing any file produces zero duplicate jobs.
5. A worker killed mid-file resumes without data loss or corruption.
6. A Canadian or Mexican job never appears under `country=US`.
7. Removing OpenJobData leaves the API, DB, and frontend compiling and passing tests
   (proves the connector abstraction holds).
8. p95 `/api/v1/search` < 300 ms at ≥ 1M active jobs.
9. Every rejected row has a stored, queryable reason.
10. No secret value exists in any tracked file.
