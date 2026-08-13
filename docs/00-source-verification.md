# 00 — OpenJobData Source Verification (EVIDENCE, NOT ASSUMPTION)

**Verified:** 2026-08-10 by direct inspection of the live bucket.
**Rule:** nothing in this file is inferred. Every claim below was produced by listing or
reading the actual remote files. Anything still unknown is in §10.

## 1. Access mechanism (VERIFIED)

The resource is a **Hugging Face Storage Bucket** (`repoType: "bucket"`), *not* a Dataset repo.
This matters: the normal `huggingface.co/datasets/...` and `/resolve/main/...` paths **404**.

| Probe | Result |
|---|---|
| `GET /api/buckets/Invicto69/Jobs-Dataset-bucket` | `200` — `repoType:"bucket"`, `private:false`, `size:32471749004`, `totalFiles:172` |
| `GET /buckets/.../resolve/main/README.md` | `404` (resolve API not supported for buckets) |
| `GET /api/buckets/.../tree/main` | `200` but `[]` (tree API not populated for buckets) |
| `huggingface_hub.HfFileSystem().ls("buckets/...")` | **works** — this is the access path |

**Canonical access:** fsspec via `HfFileSystem`, URI scheme `hf://buckets/Invicto69/Jobs-Dataset-bucket`.

```python
from huggingface_hub import HfFileSystem

fs = HfFileSystem()  # no token required — bucket is public
fs.ls("buckets/Invicto69/Jobs-Dataset-bucket/data/minimal/changes")
```

- **Authentication: NOT required.** Anonymous listing and reads both succeeded.
- **Rate limits: not documented and not empirically probed.** Treat as unknown → see §10.
- `HfFileSystem` is seekable, so **PyArrow column projection issues HTTP range requests**
  and genuinely avoids transferring unselected columns. Measured in §5.

## 2. License (VERIFIED)

`README.md` at bucket root declares `license: mit`. The companion sample dataset
`Invicto69/Job-dataset-samples` also declares MIT.

> MIT covers the dataset artifact. It does **not** by itself grant rights over third-party
> job content, employer trademarks, or ATS terms of service. Legal review is a §10 item.

## 3. File structure (VERIFIED by `fs.ls`)

```
hf://buckets/Invicto69/Jobs-Dataset-bucket/
├── .gitattributes                       3,121 B
├── README.md                              935 B
└── data/
    ├── full/          12 base shards  part-0..part-11.parquet   (~10.7 GB)
    │   └── changes/   73 daily deltas YYYY-MM-DD.parquet        (~20.2 GB, median 282 MB)
    ├── minimal/       12 base shards  part-0..part-11.parquet   (~0.18 GB)
    │   └── changes/   72 daily deltas YYYY-MM-DD.parquet        (~434 MB, median 6.3 MB)
    └── companies/
        └── companies.parquet           16,097,068 B — 109,166 rows
```

Delta filenames are exactly `YYYY-MM-DD.parquet`. Range observed: `2026-05-26` … `2026-08-08`.

## 4. Schema (VERIFIED by reading Parquet footers)

### 4a. `jobs` — minimal variant (14 columns, flat)

```
id string | job_id string | company_id int32 | title string | department string
employment_type string | workplace_type string | country string | is_remote bool
posted_at timestamp[us,tz=UTC] | apply_url string | fetched_time timestamp[us,tz=UTC]
status string | close_time timestamp[us,tz=UTC]
```

### 4b. `jobs` — full variant = minimal **+ 2 columns**

```
entire_json     string  (double-encoded JSON) — raw scraper payload
job_model_json  string  (double-encoded JSON) — normalized job model
```

> **Both JSON columns are double-encoded**: the Parquet value is a JSON *string* whose content
> is itself JSON. `json.loads()` must be applied twice. Verified — a single decode returns `str`.

### 4c. `job_model_json` decoded structure (VERIFIED, n=3,001 rows parsed)

Top-level keys, present on 100% of parsed rows:

```
apply_url, ats_provider, compensation, department, description_html, description_plain,
employment_type, expires_at, job_id, location, metadata, posted_at, requirements,
responsibilities, seniority, title
```

`location` (dict, **100% present, 0 nulls** across 3,001 rows):

```json
{"city": "Needham", "country": "United States", "is_remote": false,
 "postal_code": null, "raw_location_text": "Needham",
 "state": "Massachusetts", "workplace_type": "onsite"}
```

`compensation` (present on 561/3,001 = 18.7%):

```json
{"benefits": [], "currency": "USD", "interval": "annually",
 "max_amount": 170000.0, "min_amount": 120000.0}
```

`expires_at` present on 299/3,001 = 10.0%.

**`state` is inconsistently encoded** — observed both `"Massachusetts"` and `"OH"` in the same
file. `raw_location_text` also carries leading whitespace and sometimes a bare state
(`"            South Carolina"`) with `country: null`. This is the concrete justification for
`LocationNormalizer` rather than string matching.

### 4d. `companies.parquet` (VERIFIED — 109,166 rows)

```
id int64 | name | website | ats | slug | unique_id | career_url | founded double
size | locality | region | country | industry | linkedin_url | linkedin_id
```

Values are **lowercased** (`country: "united states"`, `region: "vermont"`).
`unique_id` looks like `adp:33574b24-...:wfn`, `breezy:adbro`.

## 5. THE CRITICAL ARCHITECTURAL FINDING — variant choice

**The `minimal` variant has no city, no state, no salary, and no description.**
It carries only `country` as a free-text string. Every location/salary/description
requirement in the product spec is therefore **unsatisfiable from `minimal`**.

Per-column byte cost, measured from Parquet footer metadata on `2026-08-08` (81,149 rows):

| Column | Compressed | Share |
|---|---:|---:|
| `entire_json` | 118.07 MB | 49.5% |
| `job_model_json` | 114.96 MB | 48.2% |
| all 14 scalar columns combined | 5.3 MB | 2.2% |
| **full file total** | **238.4 MB** | 100% |

**Decision: ingest the `full` variant with column projection that EXCLUDES `entire_json`.**

- Cost: **~120 MB/day**, not 238 MB/day. Column projection halves it for free.
- Backfill: ~10.7 GB full base → **~5.4 GB** with the same projection.
- `entire_json` is the raw scraper payload; `job_model_json` is the normalized model and is
  the only one we need. `entire_json` is never read by this platform.

`description_html` embeds **base64 images inline**, which is why these columns are so large.
Sanitizing and stripping `data:` URIs at normalization time is required.

## 6. Data volume and US share (VERIFIED, delta 2026-08-08)

81,149 rows in one day. `country` distribution:

| country | rows |
|---|---:|
| United States | **62,425** |
| *(empty string)* | 3,331 |
| United Kingdom | 2,838 |
| Canada | 1,825 |
| India | 909 |
| *(null)* | 442 |
| `REMOTE` (junk) | 270 |

US share ≈ 77%. Of the 62,425 US rows: **29,546 active**, 32,879 closed.

This directly validates the "no artificial top-100 limit" requirement — a single day's delta
carries ~62k US rows.

## 7. Timestamp semantics (VERIFIED — these break naive assumptions)

Measured on delta `2026-08-08`:

| field | observed range | meaning |
|---|---|---|
| `posted_at` | `2013-06-13` → **`2026-09-11`** | source posting time. **Nulls: 15,698 / 81,149 (19.3%)** |
| `fetched_time` | `2026-05-24` → `2026-08-08 14:00` | pipeline ingest time. **Not** the delta date |
| `close_time` | `2026-08-08 12:50` → `14:00` | *detected-closed* time, only on `status='closed'` |

**Four hard consequences:**

1. **`posted_at` contains future dates** — max was `2026-09-11`, 34 days ahead of the file date.
   A future-dated `posted_at` must be quarantined, never shown as "posted 18 minutes ago".
2. **`posted_at` is NULL for 19.3% of rows.** Freshness logic must not assume it exists.
3. **The delta date is NOT the posting date.** In the `2026-08-08` file only **806** US rows
   have `posted_at >= 2026-08-08`. The other ~61.6k are re-detections and closures.
   This is exactly the trap the spec warns about.
4. **`fetched_time` on a delta row is the ORIGINAL fetch time** (min `2026-05-24`), not the
   delta date. So `fetched_time` is neither our `first_seen_at` nor our `fetched_at`.

`close_time` clusters inside the file's own generation window, so it means "we noticed it
closed", not "the employer's stated close date". The employer's date, when present, is
`job_model_json.expires_at` (10% coverage). These map to two **different** DB columns
(`closed_at` vs `close_at`).

## 8. Identity and deduplication keys (VERIFIED)

- `id` — format `{ats}:{company_slug}/{job_id}`, e.g. `jobvite:aceservicecompany/olcCAfwt`.
  **0 duplicates** in 81,149 rows. This is the stable source primary key.
- `job_id` — **2,705 duplicates** in the same file. **Never dedupe on `job_id` alone.**
- `apply_url` — 110 nulls (0.14%).

Source is multi-ATS. Top providers by row count in one day:

```
workday 21,807 | adp 9,149 | oracle_hcm 6,580 | smartrecruiters 6,441 | ultipro 5,733
paycom 5,135 | icims 4,526 | greenhouse 3,529 | dayforce 3,258 | jazzhr 2,283
```

> Note: OpenJobData **already aggregates Greenhouse, Lever, Ashby, Workday and others.**
> When those are added as direct connectors later, they will collide with OpenJobData rows.
> The Level 1–4 dedup chain is not speculative future-proofing; it is required on day one
> of the second source.

## 9. Update cadence and gaps (VERIFIED — re-checked 2026-08-10 16:00Z)

> **CORRECTION.** An earlier reading of this bucket at ~06:00Z on 2026-08-10 showed
> `2026-08-08` as the newest delta and was written up as a "~2 day publication lag".
> That was wrong. Re-checking at 16:00Z shows same-day publication; what looked like lag
> was a **skipped day** (`2026-08-09`) plus a file that had not yet been uploaded that
> morning. The corrected numbers are below.

Publication is **same-day**. Each `YYYY-MM-DD.parquet` is uploaded on that same date:

| file date | uploaded | lag |
|---|---|---:|
| 2026-08-05 | 12:52 UTC | 0 d |
| 2026-08-06 | 11:23 UTC | 0 d |
| 2026-08-07 | 10:40 UTC | 0 d |
| 2026-08-08 | 14:15 UTC | 0 d |
| 2026-08-10 | 06:33 UTC | 0 d |

The **upload hour varies widely (06:33–14:15 UTC)**, so a fixed-time cron would sometimes
run before the day's file exists. Poll on an interval and let `discover()` decide.

**Skipped days are real and differ per variant** (76-day window, 2026-05-26 … 2026-08-10):

| variant | files | missing dates |
|---|---:|---|
| `minimal` | 73 | `2026-06-25`, `2026-07-16`, `2026-08-02`, `2026-08-09` |
| `full` | 74 | `2026-07-16`, `2026-08-02`, `2026-08-09` |

`full` has a `2026-06-25` delta that `minimal` does not. A connector must therefore discover
gaps **per variant** and never infer one variant's availability from the other.

**Two consequences:**

1. The publisher **skips days**. A cron that just processes "yesterday" silently loses a
   whole day forever. `discover()` must **list the remote directory and diff it against
   processed watermarks**, never iterate a date range assuming contiguity.
2. Data arrives in **one batch per day**, so "posted in the last hour" is still bounded by
   daily batching even though publication is same-day. Sub-hour freshness requires direct
   ATS connectors (Phase 2). The UI must derive freshness from `posted_at` and be honest
   about detection time.

### 9a. File versioning (VERIFIED)

`HfFileSystem.ls(..., detail=True)` returns per file:

```json
{"name": "...", "size": 2300917, "type": "file",
 "xet_hash": "329c91f2b854fa8095585b6915823c253aff8a727419a3f1b76c89b8ffc66606",
 "mtime": "2026-08-10 06:29:23.840000+00:00",
 "uploaded_at": "2026-08-10 06:33:14.661000+00:00"}
```

**`xet_hash` is a real content hash** and is used as the checkpoint version marker, so a
re-published or corrected file is detected and re-ingested while a byte-identical one is
skipped. `size` is the fallback when a source does not expose a hash.

## 10. STILL UNKNOWN — must be resolved before production, NOT invented

These are configuration/interface points in the code, deliberately not hard-coded guesses:

| # | Unknown | How it is handled in code |
|---|---|---|
| 1 | HF anonymous **rate limits / throttling** for bucket reads | `OPENJOBDATA_MAX_CONCURRENCY` + retry/backoff; measure in M6 |
| 2 | ~~Publication time of day~~ **RESOLVED**: same-day, variable 06:33–14:15 UTC | scheduler polls `discover()` on an interval; a fixed cron would miss early/late files |
| 3 | Whether a delta is ever **re-published/corrected** after first appearance | connector records `xet_hash` (a real content hash, §9a) as the checkpoint version; a changed hash re-ingests, idempotent upsert makes that safe |
| 4 | Retention: are old `changes/` files ever **deleted**? | never rely on remote history; our DB is the system of record |
| 5 | Exact semantics of `status='closed'` vs employer removal | modelled as two distinct states: `EXPIRED` (source says closed) vs `REMOVED` (vanished from source) |
| 6 | Whether `entire_json` holds fields absent from `job_model_json` | not needed for v1; excluded by projection. Revisit only with evidence |
| 7 | Legal review of MIT-dataset vs downstream ATS ToS for **re-display** of descriptions | product/legal gate before public launch |
| 8 | Base `part-*.parquet` **overlap semantics** with `changes/` (is base a snapshot superset?) | backfill is upsert-by-`external_id`, so overlap is harmless either way |

## 11. Reproducing this verification

```bash
python scripts/verify_source.py
```

Re-runs every probe in this document against the live bucket and prints a pass/fail table.
Run it before trusting any claim above — the source can change.
