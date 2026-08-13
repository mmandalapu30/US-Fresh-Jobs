# Milestone 3 — OpenJobDataConnector

**Status: COMPLETE and verified against the live bucket.** 278 tests passing (78 new),
3 live network tests green, lint/format/layering clean.

The first real `SourceConnector`. All of it lives in
`workers/ingestion/ingestion/connectors/openjobdata/` — the layering guard proves nothing
leaked out.

---

## 1. A correction to the source documentation

An earlier reading of the bucket at ~06:00Z on 2026-08-10 showed `2026-08-08` as the newest
delta, and I wrote that up as a **"~2 day publication lag"**. That was wrong.

Re-checking at 16:00Z shows **same-day publication**:

| file date | uploaded | lag |
|---|---|---:|
| 2026-08-05 | 12:52 UTC | 0 d |
| 2026-08-06 | 11:23 UTC | 0 d |
| 2026-08-07 | 10:40 UTC | 0 d |
| 2026-08-08 | 14:15 UTC | 0 d |
| 2026-08-10 | 06:33 UTC | 0 d |

What looked like lag was a **skipped day** (`2026-08-09`) plus a file that had not yet been
uploaded that morning. `docs/00-source-verification.md` §9 is corrected.

Two consequences, both now encoded in the connector:

- The **upload hour varies (06:33–14:15 UTC)**, so a fixed-time cron would sometimes run
  before the day's file exists. Discovery polls instead.
- Data still arrives in **one batch per day**, so sub-hour freshness still requires direct
  ATS connectors. That part of the earlier assessment stands.

Also newly verified: `HfFileSystem.ls(detail=True)` exposes **`xet_hash`, a real content
hash**, which is now the checkpoint version marker. A corrected republish is detected;
a byte-identical file is skipped.

---

## 2. Files created

| File | Purpose |
|---|---|
| `connectors/openjobdata/schema.py` | Paths, column projections, double-JSON decoding, filename parsing |
| `connectors/openjobdata/connector.py` | The connector: discover, fetch, validate, normalize, archive |
| `connectors/openjobdata/__init__.py` | Public surface |
| `tests/connectors/openjobdata/test_connector.py` | 78 tests — offline fixtures, contract suite, live |

---

## 3. Design decisions, each traceable to a measured fact

| Decision | Measured fact behind it |
|---|---|
| `discover()` **lists** the directory | Publisher skips days: `2026-07-16`, `2026-08-02`, `2026-08-09` missing from `full`. A generated range loses them and never notices a backfill |
| `etag = xet_hash` | Real content hash exposed by the bucket; detects in-place corrections |
| Projection excludes `entire_json` | It is 49.5% of the file (118 MB of 238 MB) and is never read. Halves the transfer |
| `fetch()` streams by row-group batch | One file is ~28k–81k rows; batching 8 groups avoids ~95 round trips *and* avoids materialising 120 MB |
| `validate()` accepts NULL `posted_at` | 19.3% of real rows. Rejecting them would discard a fifth of the dataset |
| `validate()` accepts **future** `posted_at` | Structural validation must not apply freshness policy; that is a shared service so all sources are judged alike |
| `normalize()` keeps location **raw** | The source emits `"Michigan"` and `"OH"` — *and* `"Quebec"` and `"Maharashtra"`. Resolving that is `LocationNormalizer`'s job (M5) |
| `tbc` → `None` | ~41% of rows. Passing it through would let downstream mistake "unknown" for on-site |
| `""` → `None` | 3,331 empty `country` values in one file; `""` and NULL both mean unknown |
| `close_at` ≠ `closed_at` | `expires_at` is the **employer's** stated expiry (10% coverage); `close_time` is when the **source** noticed removal. Different facts |
| Company lookup cached per instance | 109,171 companies. A per-row remote lookup would be absurd |
| Company-file outage is non-fatal | Jobs keep `company_external_id` and can be enriched later; ingestion should not abort |

---

## 4. Verified against the live bucket

`pytest -m network` runs three tests against the real source. Beyond that, a full
end-to-end run on 2026-08-10:

```
=== DISCOVER ===
  74 delta files in 0.7s
  oldest 2026-05-26   newest 2026-08-10
  gaps detected: ['2026-07-16', '2026-08-02', '2026-08-09']
  newest: 2026-08-10.parquet  85.2 MB   version 3f40cf5cd956ff2d... (xet hash)

=== FETCH + VALIDATE + NORMALIZE (streaming, projected) ===
  6,000 rows   valid 5,999   rejected 1 (MISSING_TITLE)
  countries: United States 3,875 | United Kingdom 463 | Australia 302 | India 117
  state values: CA 60 | Maharashtra 58 | Quebec 56 | FL 51 | TX 44 | OH 42

=== A REAL NORMALIZED ROW ===
  external_id       'jobscore:letsplaysports/ad1R15LUnkfBz1HpgGE2e_'
  title             "General Maintenance - Let's Play Soccer, Colorado Springs"
  company_name      "Let's Play Soccer"
  raw_city/state    'Centennial' / 'CO'
  salary            18.0-23.0 USD hourly
  posted_at         2026-07-23 13:57:44 UTC
  source_fetched_at 2026-07-24 08:40:38 UTC      <- distinct from posted_at
  close_at          None                          <- employer stated none
  closed_at         2026-08-10 05:39:38 UTC       <- source detected removal
  raw_status        'closed'
```

**New finding for Milestone 5:** the `state` field carries **non-US values** —
`Maharashtra` (India) and `Quebec` (Canada) rank among the most common. `LocationNormalizer`
must reject them rather than assume a populated `state` implies a US job.

**Performance note:** ~174 rows/s in that run, so a 28k-row file takes ~3 minutes. The
bottleneck is `to_pylist()` plus per-row double-JSON decode. Acceptable now; Milestone 4
will process Arrow batches with Polars instead of per-row Python dicts.

---

## 5. Tests

```
278 passed  (200 from M1-M2, 78 new)
└─ tests/connectors/openjobdata/test_connector.py   78
   ├─ paths, filename parsing, nested-JSON decoding
   ├─ discover: gaps, ordering, ignored entries, versioning, typed errors
   ├─ projection: entire_json excluded, variant switching, opt-in
   ├─ fetch: laziness, row indices, unique ids, typed errors
   ├─ validate: NULL/future posted_at accepted; missing title/url rejected
   ├─ normalize: raw location, tbc, empty country, the two closure timestamps
   ├─ companies: cached once, outage degrades gracefully
   ├─ archiving: key layout, archive-then-read, idempotent
   ├─ contract suite (inherited from SourceConnectorContract)
   └─ live: 3 tests, `-m network`
```

Offline fixtures reproduce the real schema exactly, including double-encoded
`job_model_json` and the awkward values. So if upstream changes shape, the **live** tests
fail while the offline ones pass — which is precisely how the divergence gets noticed.

### Two real bugs the tests caught

1. **Open failures escaped untyped.** `_open_for_read()` sat outside the `try` in
   `fetch()`, so a missing or unreachable file surfaced as a raw `FileNotFoundError`
   instead of the `SourceUnavailableError` the ingestion task knows how to retry.
2. A fixture gap where the contract suite fetched a listed-but-bodyless file.

---

## 6. How to run

```bash
pytest workers/ingestion/tests/connectors -q     # 78 offline tests
pytest -m network -q                             # against the real bucket
python scripts/verify_source.py                  # re-verify the documented facts
make layering                                    # prove nothing leaked
```

---

## 7. Acceptance criteria — verified

| # | Criterion | Evidence |
|---|---|---|
| 1 | Implements `SourceConnector` fully | inherits and passes `SourceConnectorContract` |
| 2 | Works against the **real** bucket | 3 live tests + full E2E run |
| 3 | Discovery lists rather than generates | skipped days absent, not invented |
| 4 | Skipped days detected and logged | `gaps=['2026-07-16','2026-08-02','2026-08-09']` |
| 5 | Non-delta entries ignored, not fatal | README/`.tmp` filtered |
| 6 | Content hash used as checkpoint version | `etag == xet_hash` |
| 7 | `entire_json` never fetched | asserted; opt-in available |
| 8 | Double-encoded JSON decoded | asserted, incl. that one `loads()` is insufficient |
| 9 | `fetch()` streams | returns an Iterator |
| 10 | NULL `posted_at` accepted, never invented | asserted |
| 11 | Future `posted_at` preserved, not clamped | asserted |
| 12 | `posted_at` ≠ `source_fetched_at` | asserted, and visible in the live row |
| 13 | `close_at` ≠ `closed_at` | asserted both ways |
| 14 | Location kept raw for the normalizer | no `state_code` on `NormalizedJob` |
| 15 | `tbc` and `""` become `None` | asserted |
| 16 | Rejections carry reasons and row location | `"row 3 of ..."` |
| 17 | Company lookup cached; outage non-fatal | asserted |
| 18 | Archive-then-read avoids re-download | asserted |
| 19 | Source errors are typed | `SourceUnavailableError` on list and open |
| 20 | No provider knowledge outside the connector | layering guard green, 52 files |

---

## 8. Next — Milestone 4

The Parquet pipeline: Polars/Arrow batch processing instead of per-row `to_pylist()`,
wired to `SyncRepository` so rejections land in `sync_errors` with reasons and checkpoints
commit with their data. That is where the ~174 rows/s becomes a throughput target rather
than an observation.
