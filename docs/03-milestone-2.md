# Milestone 2 — Source abstraction, object storage, sync tracking

**Status: COMPLETE and verified.** 200 tests passing (82 new), lint/format clean, layering
guard green.

Scope: the `SourceConnector` interface, object storage with two interchangeable backends,
the sync/checkpoint/rejection repository, the Celery application, and the CI check that
keeps provider knowledge inside its connector.

Still **no OpenJobData code** — that is Milestone 3. Building the interface first is what
makes the "do not hard-code source logic throughout the application" requirement
achievable rather than aspirational.

---

## 1. Files created

| File | Purpose |
|---|---|
| `workers/ingestion/ingestion/storage/base.py` | `ObjectStore` Protocol, `ObjectMetadata`, key builder |
| `workers/ingestion/ingestion/storage/local.py` | Filesystem backend (atomic writes, traversal-safe) |
| `workers/ingestion/ingestion/storage/s3.py` | S3/MinIO/R2 backend (multipart, paginated listing) |
| `workers/ingestion/ingestion/storage/__init__.py` | `get_object_store()` factory |
| `workers/ingestion/ingestion/connectors/base.py` | `SourceConnector` Protocol + domain models |
| `workers/ingestion/ingestion/repositories/sync.py` | Runs, checkpoints, rejections |
| `workers/ingestion/ingestion/celery_app.py` | Celery app tuned for long ingestion tasks |
| `scripts/check_layering.py` | Source-isolation guard |
| `workers/ingestion/tests/test_storage.py` | 33 tests, both backends |
| `workers/ingestion/tests/test_sync_repository.py` | 24 tests against real PostgreSQL |
| `workers/ingestion/tests/test_connector_contract.py` | 22 tests + reusable contract suite |
| `workers/ingestion/tests/test_layering.py` | 3 tests — the guard is itself tested |

---

## 2. The `SourceConnector` interface

Exactly the method set the specification names:

```python
class SourceConnector(Protocol):
    def get_source_name(self) -> str: ...
    def get_capabilities(self) -> ConnectorCapabilities: ...
    def discover(self, *, since: date | None = None) -> Sequence[SourceFile]: ...
    def fetch(self, source_file: SourceFile) -> Iterator[RawRecord]: ...
    def fetch_incremental(self, *, since: date | None = None) -> Iterator[RawRecord]: ...
    def validate(self, record: RawRecord) -> ValidationResult: ...
    def normalize(self, record: RawRecord) -> NormalizedJob: ...
```

Four design decisions, each traceable to a measured fact about the real source:

| Decision | Reason |
|---|---|
| `discover()` returns **listed** units, not a generated date range | The publisher skips days (verified: 3 gaps), and the gap sets differ per variant |
| `SourceFile.checkpoint_key()` includes a **version** (etag/size) | A source can correct a file in place; matching on path alone would skip the fix |
| `fetch()` returns an **Iterator** | One unit holds ~81,000 records; materialising them all wastes memory needed for Parquet buffers |
| `NormalizedJob` keeps location **raw** (`raw_state`, `raw_country`) | A connector reports what the source said; it does not decide US-ness. Classification is a shared service so every source is judged identically |

`ValidationResult.reject()` raises if given no reason — rejecting a row without recording
why is precisely what the spec forbids, so it is impossible to express.

### Reusable contract suite

`SourceConnectorContract` in the test module is a base class every future connector
subclasses. It asserts source-name stability, that discovered units have unique checkpoint
keys, that `fetch()` yields `RawRecord`s, and that validate→normalize round-trips. Adding
Greenhouse or Lever therefore cannot skip these checks.

---

## 3. Object storage

One Protocol, two backends, **one test suite parametrised across both** — if they diverge,
code that passes in tests would fail in production.

```
raw/{source}/{variant}/{kind}/{filename}
raw/acme-source/full/changes/2026-08-08.parquet
```

Source and kind lead the path so a lifecycle rule can expire one source's archive without
touching another's, and date-named keys sort chronologically under prefix listing.

`LocalObjectStore` is not a toy: it writes to a temp file and `os.replace`s it, so a crash
mid-write cannot leave a truncated object that later looks complete. It also refuses keys
that escape the storage root — archive keys are partly source-derived, so traversal is a
real concern.

Selecting a backend is configuration: `OBJECT_STORAGE_URL=file:///path` gives the local
backend, anything else gives S3. A developer without Docker can run the whole pipeline.

---

## 4. Sync tracking — the durability layer

Three guarantees, all verified against real PostgreSQL:

**One active run per source.** A partial UNIQUE index; `start_run` raises the typed
`SyncRunActiveError` rather than a driver exception. `reclaim_stale_runs()` handles the
case the index otherwise creates: a worker killed by OOM leaves a `RUNNING` row that would
block every future run forever. Reclaiming marks it `FAILED` — the lock is released and the
incident stays visible, rather than being silently swallowed.

**Per-file checkpoints.** `checkpoint_file()` accepts an existing connection so progress
commits *in the same transaction as the data it describes*. This is the subtle part: if a
checkpoint could commit independently, a crash would leave it claiming rows that were
rolled back, and the resumed run would skip them permanently. There is a test that opens a
transaction, checkpoints, raises, and asserts the checkpoint rolled back with it.

**No silent drops.** `rejection_buffer()` batches rejections and flushes on exit — including
when the body raises, so a crash mid-file still records why everything up to that point was
rejected.

---

## 5. The layering guard

```bash
make layering          # verify
make layering-rules    # show the rules
```

Provider tokens (`openjobdata`, `huggingface`, `hf://`, `invicto69`) may appear only in
that provider's connector package, its tests, or config/docs/scripts where naming a source
is the point. Anywhere else fails the build.

**It found three real leaks on first run, all fixed properly rather than exempted:**

1. `tests/test_migrations.py` used `'openjobdata'` as a fixture value — the schema must not
   know or care which provider a row came from. Now `'source-a'`/`'source-b'`.
2. `logging.py` enumerated `huggingface_token` in its redaction list. Replaced with pattern
   matching (`*_token`, `*_secret_key`, ...), which is both provider-agnostic **and**
   strictly safer: any future credential is redacted the day it is added.
3. A docstring example in `storage/base.py` named the provider. Now neutral.

The guard is itself tested: one test plants a violation in a non-exempt file and asserts it
is detected. A guard that silently stops working is worse than no guard.

CI runs it as a **blocking** job, not advisory.

---

## 6. Celery configuration

Tuned for a workload where one task runs for minutes and downloads ~120 MB. Defaults meant
for short web tasks would lose work:

| Setting | Value | Why |
|---|---|---|
| `task_acks_late` | `True` | A worker dying mid-file redelivers the task instead of losing it. Safe because ingestion is idempotent |
| `task_reject_on_worker_lost` | `True` | Same, for hard kills |
| `worker_prefetch_multiplier` | `1` | Long uneven tasks — prefetching leaves a worker sitting on work it cannot start |
| `worker_max_tasks_per_child` | `50` | PyArrow buffers fragment memory over time |
| `task_soft_time_limit` | `3600` | A stuck download must not hold a slot forever |

`worker_process_init` disposes inherited database engines after fork: a forked child
inheriting the parent's socket means two processes writing to one connection, which
corrupts the protocol stream.

---

## 7. Tests

```
200 passed  (118 from M1, 82 new)
├─ workers/ingestion/tests/test_storage.py            33   both backends, same suite
├─ workers/ingestion/tests/test_sync_repository.py    24   real PostgreSQL
├─ workers/ingestion/tests/test_connector_contract.py 22   Protocol + reusable contract
└─ workers/ingestion/tests/test_layering.py            3   the guard, including detection
```

Notable coverage of spec-named cases:

- **Case 12 — reprocessed source file:** `already_processed()` returns `True` for the same
  version, `False` for a republished one.
- **Case 13 — worker crashes during ingestion:** progress survives; a checkpoint inside a
  rolled-back transaction does not.
- **Case 14 — database temporarily unavailable:** `SyncRunActiveError` and stale-run
  reclamation cover the resulting stuck-lock scenario.

### Three real bugs the tests caught

Worth recording, because each would have surfaced in production:

1. **Invalid JSON on oversized payloads.** Truncating the JSON *text* of a large rejected
   record produced a syntactically invalid document that `jsonb` rejected — losing the
   rejection record entirely, the exact opposite of the requirement. Now wrapped in a valid
   envelope carrying a preview plus the original size.
2. **`SourceFile` was unhashable.** Frozen but carrying a `dict`, so it could not go in a
   set — and de-duplicating discovery results is an obvious thing to want. `metadata` is now
   excluded from equality and hashing; a unit's identity is its path plus version.
3. **The layering guard's own test tripped the guard.** Resolved with a narrow, commented
   exemption rather than by weakening the rule.

---

## 8. How to run

```bash
make test            # 200 tests
make layering        # source-isolation guard
make check           # lint + layering + typecheck + test

pytest workers/ingestion/tests -q          # this milestone only
pytest -m "not integration" -q             # no database required
```

---

## 9. Acceptance criteria — verified

| # | Criterion | Evidence |
|---|---|---|
| 1 | `SourceConnector` Protocol has the specified methods | asserted by name |
| 2 | An incomplete implementation fails `isinstance` | asserted |
| 3 | A reusable contract suite exists for future connectors | `SourceConnectorContract`, proven against a fake |
| 4 | Rejection without a reason is impossible | `ValidationResult.reject()` raises |
| 5 | Connector output keeps location raw | `NormalizedJob` has no `state_code`/`country_code` |
| 6 | Both storage backends satisfy one Protocol | 33 tests parametrised across both |
| 7 | Local writes are atomic | failing mid-write leaves no object |
| 8 | Storage refuses path traversal | 3 escape attempts rejected |
| 9 | Reprocessing the same file version is skipped | `already_processed` |
| 10 | A republished file is **not** skipped | etag-aware |
| 11 | Progress survives a crash | checkpoint readable after simulated death |
| 12 | Checkpoints commit atomically with their data | rollback test |
| 13 | Stale run lock is reclaimable and stays visible | marked `FAILED`, not deleted |
| 14 | Every rejection reason is storable | all 14 enum members round-trip |
| 15 | Rejections flush even when the body raises | asserted |
| 16 | Oversized payloads stored as valid JSON | length-bounded envelope |
| 17 | No provider knowledge outside its connector | guard green over 45 files, blocking in CI |
| 18 | The guard detects a planted violation | asserted |
| 19 | Celery is configured for long, crash-tolerant tasks | `acks_late`, prefetch 1, child recycling |
| 20 | Lint, format and layering all clean | verified |

---

## 10. Environment

Unchanged from Milestone 1, plus:

| Variable | Note |
|---|---|
| `OBJECT_STORAGE_URL` | `file:///path` now selects the local backend; anything else is S3 |

New dependencies: `boto3`, `tenacity`, `celery[redis]`, and `moto[s3]` for tests.

**Docker remains unverified.** It is not installed on this machine (no PATH entry, no
uninstall registry record, and WSL2 — which Docker Desktop requires — is absent). The
compose file and both Dockerfiles are authored but unbuilt; CI's `docker-build` job is what
will first prove them.

---

## 11. Next — Milestone 3

`OpenJobDataConnector`: the first real implementation, living entirely inside
`connectors/openjobdata/`. It will implement `discover()` against the live bucket listing
(handling the verified skipped days), archive files to object storage before parsing, and
subclass `SourceConnectorContract` so it inherits every check above.
