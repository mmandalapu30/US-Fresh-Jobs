# Milestones 4–11 — Pipeline, database load, API, and UI

**Status: working end to end on live data.** Real jobs flow from the Hugging Face bucket
into PostgreSQL, out through the API, and onto a rendered Next.js UI.

This covers a lot of ground in one pass because the goal was a working vertical slice —
data visible in the database *and* the UI — rather than each milestone in isolation.

---

## 1. What was built

| Component | File | Milestone |
|---|---|---|
| `LocationNormalizer` | `services/location.py` | 5 |
| `FreshnessService` | `services/freshness.py` | 7 |
| `DedupeService` (L1–L4) | `services/dedupe.py` | 6 |
| `JobLoader` (bulk idempotent write) | `repositories/jobs.py` | 8 |
| `IngestionPipeline` | `pipeline/process.py` | 4, 8 |
| Ingestion CLI | `scripts/ingest.py` | 8 |
| Read repository + job endpoints | `apps/api/app/...` | 9 |
| Next.js UI (5 pages) | `apps/web/src/...` | 11 |

---

## 2. Bugs found by running it for real

Every one of these was invisible in unit tests and surfaced only by loading real data.

### 2.1 Provenance was never written — idempotency silently broken

The first successful-looking run reported "0 inserted" while the database gained 19,728
jobs and **1** `job_sources` row.

`RETURNING id` does not survive an executemany — the driver discards the result sets — so
the follow-up insert that binds each job to its source ran against an empty id list.
Without `job_sources`, the L1 uniqueness guarantee does not exist, and a re-run would have
inserted every job again.

**Fix:** generate `canonical_job_id` (a UUID) client-side, insert it with the job, then
`JOIN` on it for provenance and events. No reliance on `RETURNING`, and no assumption
about row ordering — which PostgreSQL does not guarantee for multi-row inserts anyway.

**Verified:** second run over the same file → `0 inserted, 19,728 unchanged`, counts
identical.

### 2.2 Transposed salary ranges aborted whole batches

The source ships `min_amount: 85000` with `max_amount: 60000`. The `jobs_salary_order`
CHECK rejected it and took the entire 1,000-row batch with it.

**Fix:** order the pair rather than discard the job. A transposed range is still readable
information; losing a real job over it is worse. Implausible values (negative, absurd) are
dropped to NULL.

### 2.3 State codes in the city field

Rows arrived with `city = "TX"` and no state, producing jobs whose "city" was a state code.

**Fix:** promote a bare two-letter code from city to state. Deliberately narrow — full
state *names* are genuinely ambiguous as city names (Wyoming MI, Delaware OH), so only the
unambiguous two-letter form is promoted.

### 2.4 Canadian jobs in a US-only feed — found by looking at the UI

The rendered homepage showed **"Cashier · Farm Boy · Ottawa"**. Farm Boy is a Canadian
grocery chain. Investigation found 317 Toronto, 155 Montreal, 81 Ottawa and 45 Mississauga
jobs, all stored with `country_code = 'US'`.

The cause: the source's per-job `country` field said "United States", and the normalizer
trusted it whenever no state contradicted it.

Three layered fixes, in increasing order of robustness:

1. **Definitively foreign cities** (`NON_US_CITIES`) override a US country claim. Toronto
   and Mississauga have no significant US counterpart.
2. **Ambiguous cities with no state are not confirmed US.** Ottawa ON and Ottawa IL are
   indistinguishable without a state; withholding an unconfirmable job beats publishing a
   foreign one. Ottawa **with** `OH` is still accepted.
3. **The employer's registered country decides.** `companies.parquet` carries the
   company's own country, which is far more reliable than the per-job field. When a job
   claims the US but has no state to prove it and the employer is registered abroad, it is
   rejected.

Rule 3 is the one that matters. A curated city list can never be complete — the remaining
leaks after rules 1 and 2 were Burlington, Richmond Hill, Collingwood and Cornwall, all
Ontario towns no reasonable blocklist would contain. Using the employer's country replaces
whack-a-mole with a real signal.

### 2.5 The layering guard caught the frontend

`apps/web/.../page.tsx` contained `job.source === "openjobdata" ? …` and the footer named
the provider. Both are exactly the coupling the connector abstraction exists to prevent —
adding a second source would have required frontend edits. Both are now source-agnostic.

---

## 3. Design decisions worth stating

**Freshness is presented honestly.** Verified: the newest `posted_at` anywhere in the data
is ~3 days behind the newest delta file, and the source publishes one batch per day. So
"Posted in the last hour" is legitimately near-zero. Rather than hide that, the homepage
separates two groups — *Detected by our platform* (our clock, always available) and *By
employer posting date* (the source's clock, often stale or missing) — and a zero carries
the note "source publishes daily". The product tells the truth about its own latency.

**Descriptions render as text, not HTML.** The source's `description_html` is third-party
markup containing inline base64 images and arbitrary tags. Until a sanitisation pass lands,
injecting it would be a stored-XSS hole. Plain text is the honest interim.

**Dedupe level 3 is gated.** "Cashier" at one company in one city is routinely several
distinct openings. L3 only fires with a specific location *and* a non-generic title,
because a false merge silently deletes a real job and is invisible in metrics.

**Ambiguity resolves to rejection, not a guess.** Every rejection is stored in
`sync_errors` with a reason from a closed enum, so "we withheld it" is queryable rather
than indistinguishable from "we never saw it".

---

## 4. Verified behaviour

| Property | Evidence |
|---|---|
| Ingestion is idempotent | Re-run of the same file: `0 inserted, 19,728 unchanged` |
| No artificial row cap | 283,720 source rows processed in one run |
| Nothing silently discarded | 73,863 rejections, each with a reason |
| No non-US jobs | `SELECT count(*) FROM jobs WHERE country_code <> 'US'` → 0 |
| No invalid state codes | 0 rows outside the 50 + DC + territories |
| `posted_at` never fabricated | NULL preserved; future dates stored but flagged invalid |
| Both timestamps visible | Job detail page shows "Posted by employer" and "Detected by our platform" separately |
| Cursor pagination | `next_cursor` issued; a cursor from a different sort is rejected 422 |
| Checkpoint atomicity | Checkpoint commits in the same transaction as its rows |

---

## 5. Performance

| Stage | Observed |
|---|---|
| Discovery (74 files) | 0.7 s |
| Full pipeline throughput | ~530 rows/s |
| Re-run over known data | ~900 rows/s |
| 4 delta files (283,720 rows) | ~9 minutes |
| API `/stats` | < 100 ms |
| API `/jobs` filtered page | < 60 ms |

The pipeline bottleneck is `to_pylist()` plus per-row double-JSON decode. Moving to Polars
batch processing is the next optimisation; it was not needed to prove correctness.

---

## 6. How to run it

```bash
# 1. database
createdb jobplatform
DATABASE_URL=postgresql+psycopg://... python -m alembic -c database/migrations/alembic.ini upgrade head

# 2. ingest real jobs
python scripts/ingest.py --max-files 4

# 3. API
cd apps/api && uvicorn app.main:app --port 8765

# 4. UI
cd apps/web && API_BASE_URL=http://127.0.0.1:8765/api/v1 npm run dev
# → http://localhost:3100
```

Ports 8000 and 8010 are already used by other applications on this machine, hence 8765/3100.

---

## 7. Still outstanding

- **Docker stack unverified.** Docker Desktop is installed (per-user, under
  `%LOCALAPPDATA%\Programs\DockerDesktop`) and the CLI works, but the engine cannot start:
  the WSL2 backend is not fully enabled. `wsl --install` from an elevated prompt plus a
  reboot is the fix. Everything above was verified against a local PostgreSQL 17 instead —
  the same version the compose file pins.
- **Description sanitisation** — required before rendering source HTML.
- **Authentication, saved jobs, alerts** — Milestones 12–13.
- **`/admin` is unauthenticated.** It exposes only aggregates, but it moves behind the
  admin role when auth lands.
- **Polars batch processing** for the ~530 rows/s bottleneck.
