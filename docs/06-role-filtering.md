# 06 — Role filtering and narrowing to a data/software board

Covers the role taxonomy, ingestion-time scope filtering, industry extraction, and the
purge that narrowed the platform to data and software roles.

---

## 1. The taxonomy is derived from the data, not a template

Before writing any rules I measured what the source actually contains. Field coverage
across 182,766 real jobs:

| field | coverage | verdict |
|---|---|---|
| `title` | **100%** | the only reliable signal |
| `department` | 37.6% | secondary hint; free text (`Store`, `Telehealth`) |
| `seniority` | **5.0%** | unusable alone — `Entry`, `Entry Level`, `1-3 years`, `Not Applicable` |

The most common title terms were *manager, assistant, technician, associate, specialist,
sales, engineer, nurse, driver, teacher*. **This is the broad US labour market, not a tech
dataset.** A software-centric taxonomy would have left the large majority uncategorised, so
the original 21 categories covered healthcare, trades, transport, food service and the rest.

**Category and level are orthogonal.** "Sales Manager" is `category=sales`,
`level=MANAGER`. Collapsing them would make "any management role" and "all sales roles"
impossible to ask for separately.

---

## 2. Splitting the tech categories

`software-it` was later split three ways, because a data role, a programming role and IT
support are different products to a job seeker — and one combined category made it
impossible for the ingestion scope to name what it wanted.

| slug | includes |
|---|---|
| `data` | data science/engineering/analysis, ML/AI, BI, analytics, ETL, SQL, warehousing |
| `software` | software, developer, full-stack, front/back-end, DevOps/SRE, QA, mobile, embedded |
| `it-ops` | help desk, sysadmin, DBA, Salesforce, network, security operations |

Ordering matters — first match wins:

- `data` precedes `software`, so "Data Engineer" is data.
- Both precede the generic categories, so analytics roles are not stolen by finance or admin.
- **Neither precedes healthcare or education**: "Clinical Data Manager" stays healthcare,
  because for those roles the domain matters more than the tooling.

### 2.1 `workday` — the platform is the market

Added 2026-09-04. Workday roles were being split across three categories by whichever
ordinary word their title reached for, which is visible in the stored rows:

| title | was | is |
|---|---|---|
| `Workday Developer Sr` | software | workday |
| `Business Analyst II - Workday` | data-analytics | workday |
| `Project Manager, Salesforce or Workday experience` | it-ops | workday |

A Workday consultant, integration developer, report writer and HRIS analyst are one hiring
market — people move between those roles and not between `software` and `hr` — so the one
attribute they share was the one attribute the board could not filter on.

The rule sits **ahead of the tech block and behind healthcare and education**, which keeps
the precedence above intact: naming the platform outranks the generic noun in the title,
and a clinical or teaching domain still outranks the tooling.

`Workday` is a product name that is also an ordinary English word, so the shift-pattern
sense is excluded — "Warehouse Associate - Flexible Workday" is not an HRIS role. The guard
is a **lookbehind only** (`flexible`, `compressed`, `shortened`, `extended`, `standard`,
`4-day`): a trailing "Scheduling", "Time Tracking" or "Payroll" names a Workday module, so
guarding the right-hand side would have thrown away real titles. Same class of bug as bare
`spark` matching "SPARK AmeriCorps Member" (§6.2).

---

## 3. Ingestion-time scope filtering

```bash
INGEST_CATEGORY_ALLOWLIST=data,software     # empty = everything (the default)
INGEST_CATEGORY_BLOCKLIST=
```

The gate sits in `IngestionPipeline._prepare`, **before** dedupe hashing and the database
write. An out-of-scope job costs only the classification that already happened.

Measured on one real file:

```
28,671 rows processed → 1,576 stored
16,937 skipped as CATEGORY_NOT_ALLOWED
1,084 rows/s  (vs 530 unfiltered — 2× faster)
```

Two deliberate properties:

- **Empty means everything.** The platform's stated principle is no artificial limits, so
  narrowing is opt-in. A default that quietly meant "nothing" would have emptied the
  platform the moment the setting shipped. There is a test pinning this.
- **Skipped jobs are recorded**, with `CATEGORY_NOT_ALLOWED` as a distinct reason from
  `COUNTRY_NOT_ALLOWED`. The job is real and well-formed — it is simply outside what this
  deployment chose to keep, and the cost of a narrow scope stays visible.

---

## 4. Industry — source data that was being discarded

`companies.industry` in the source registry is **90% populated**, and the connector was
already reading it and then throwing it away. Now stored and denormalized onto `jobs`:
**98.7% coverage**.

Industry is a **second axis** to role category, not a substitute. A Registered Nurse at a
hospital and a Registered Nurse at a school share a category but not an industry, and users
filter on both.

`scripts/backfill_company_industry.py` refreshes it from the 16 MB registry in ~90 seconds
rather than a one-hour re-ingest — the registry is a lookup table, not an event stream, so
re-reading it is cheap and always safe.

---

## 5. The purge

```
Deleted 177,814 jobs in 52s → 4,952 remaining
```

Dry run by default; `--execute` requires typing the exact count. Batched at 5,000 so the
table is never locked for long — a single DELETE of 178k rows would hold locks for the
whole run, and `job_events` is partitioned so the cascade fans out per partition.

**This deliberately contradicts the platform's "never delete, only transition" rule.** That
rule protects job *history* — a posting that closed still happened. Purging an entire
category is a different act: a product decision that those roles are not part of this
platform. It is a one-off operator action, documented as such so it is not copied into the
pipeline.

---

## 6. Bugs found, and how

Every one of these was found by looking at real output, not by unit testing.

### 6.1 A regex boundary bug silently emptied categories

`\b(manufactur|biolog|recruit)\b` can **never** match "Manufacturing", "Biologist" or
"Recruiter" — the trailing boundary blocks the inflection. Thousands of jobs were being
dumped into "Other". Leading boundary only fixed it; uncategorised fell 16.1% → 13.9%.

### 6.2 Greedy alternatives

- bare `principal` filed "Principal Engineer" as school staff
- bare `tech` filed "Tech Specialist" as a tradesperson
- bare `officer` would have made "Chief Executive Officer" a security job
- bare `clerk` filed "Data Entry Clerk" as retail
- `pilot` made "Pilot Program Manager" aviation

### 6.3 "Systems Engineer" polluted the software board

Found by reading stored rows: **"Thermal and Fluid Systems Engineer" was classified as
software.** In aerospace and defence a systems engineer does requirements and integration,
not code. 1,202 of 7,139 software rows (17%) had no programming signal.

`systems engineer` and `application engineer` now fall through to `engineering`.
**Precision over recall was the deliberate choice**: a mechanical engineer on a data/software
board is worse than missing a genuine software systems engineer. Genuine software titles
survive — Platform Engineer, Solutions Architect, Integration Engineer.

### 6.4 Cursor pagination was 500-ing on every date sort

Unrelated to this feature, exposed by clicking through. asyncpg rejects a `str` for a
`timestamptz` parameter, and the SQL `CAST` does not help because the driver refuses it
before PostgreSQL sees it. Every "Next page" on the default sort was broken.

### 6.5 A self-inflicted incident

Verifying that migration `0002` was reversible, I ran the round trip **on the live
database**. That dropped the two new columns and wiped 182,766 classifications. Recovered
by re-running the backfill.

**Round-trip migrations on a throwaway database, never a populated one.** Reversibility is
worth verifying; the verification just must not be destructive.

---

## 7. What "level" honestly means

57% of jobs resolve to `UNKNOWN`, because most titles genuinely state no level. The UI says
so rather than guessing — inventing "Mid level" would be the same fabrication as inventing a
posting date. `MID` sits at 0.1% because it comes almost entirely from the source's own
5%-populated field, not from title inference.

---

## 8. Operational commands

```bash
# reclassify every stored job after a taxonomy change (~100s for 180k)
python scripts/backfill_categories.py --all

# refresh company industry from the source registry (~90s)
python scripts/backfill_company_industry.py

# see what a narrowed scope would delete (dry run)
python scripts/purge_out_of_scope.py

# apply it
python scripts/purge_out_of_scope.py --execute
```

---

## 9. Known limitations

- **"IT Systems Engineer" classifies as `engineering`**, not `it-ops`, because
  `engineering` is checked first. Immaterial while both are out of scope.
- **13.9% of the full corpus stays "Other"** — genuinely ambiguous titles such as
  "Implementation Advisor" and "Senior Specialist". Guessing would be worse.
- **Salary is published on only 7.9% of active jobs.** A source property, not a defect,
  but it caps how useful the salary filter can be.
- Category assignment is **single-label**. `job_category_map` exists for secondary
  categories but is not yet populated.
