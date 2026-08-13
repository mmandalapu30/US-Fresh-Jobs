"""JobLoader — the write path into PostgreSQL.

Everything here is built around one requirement: **ingestion must be idempotent**. Running
the same file twice, or crashing halfway and re-running, must never create a duplicate job
and must never lose one.

That is achieved by making the database the arbiter rather than application logic:

* ``job_sources (source, external_job_id)`` is a UNIQUE constraint, so a source row can map
  to exactly one job no matter how many times it is processed.
* Every write in a batch happens in **one transaction** together with its sync checkpoint,
  so a crash rolls back both and the retry sees a consistent world.
* Existing jobs are resolved in **set operations**, not per row — a 60k-row file cannot
  afford 60k round trips.

Merges (levels 2-4) attach a second provenance row to an existing job rather than deleting
anything. Nothing is ever destroyed; the merge is recorded in ``job_events``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from jobplatform_schemas import DedupeLevel, JobEventType, JobStatus
from jobplatform_shared import get_logger
from jobplatform_shared.time import utc_now

__all__ = ["JobLoader", "LoadResult", "PreparedJob"]

logger = get_logger(__name__)

#: Rows per statement. Large enough to amortise round trips, small enough that one
#: statement's parameter list stays well inside the protocol limit.
_CHUNK = 1_000


@dataclass(slots=True)
class PreparedJob:
    """A job that has passed the whole pipeline and is ready to be written."""

    source: str
    external_id: str
    title: str
    title_normalized: str

    company_name: str | None
    company_external_id: str | None

    country_code: str | None
    state_code: str | None
    city: str | None
    city_normalized: str | None
    postal_code: str | None

    remote_type: str
    employment_type: str
    seniority: str | None
    department: str | None

    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_interval: str

    posted_at: datetime | None
    posted_at_is_valid: bool
    source_fetched_at: datetime | None
    close_at: datetime | None
    closed_at: datetime | None

    status: str
    apply_url: str | None
    apply_url_canonical: str | None
    apply_url_hash: bytes | None
    job_url: str | None

    content_hash: bytes
    dedupe_fingerprint: bytes

    description_text: str | None = None
    description_html: str | None = None
    ats_provider: str | None = None
    #: Primary role category and seniority, derived by JobClassifier.
    category_slug: str = "other"
    seniority_level: str = "UNKNOWN"
    company_website: str | None = None
    company_career_url: str | None = None
    company_industry: str | None = None
    company_size: str | None = None
    #: Denormalized onto jobs for filtering without a join.
    industry: str | None = None


@dataclass(slots=True)
class LoadResult:
    """What a batch actually did. Feeds sync_runs counters and the admin dashboard."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    merged: int = 0
    companies_created: int = 0
    locations_created: int = 0
    errors: list[str] = field(default_factory=list)

    def __iadd__(self, other: LoadResult) -> LoadResult:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.merged += other.merged
        self.companies_created += other.companies_created
        self.locations_created += other.locations_created
        self.errors.extend(other.errors)
        return self


class JobLoader:
    """Bulk, idempotent writes into the job tables."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(
        self,
        jobs: list[PreparedJob],
        *,
        sync_run_id: int | None = None,
        checkpoint: Any | None = None,
    ) -> LoadResult:
        """Write a batch atomically.

        ``checkpoint`` is an optional callable taking the open connection. It runs inside
        the same transaction as the writes, so the sync checkpoint can never claim rows
        that were rolled back.
        """
        result = LoadResult()
        if not jobs:
            return result

        with self._engine.begin() as conn:
            company_ids = self._upsert_companies(conn, jobs, result)
            location_ids = self._upsert_locations(conn, jobs, result)
            self._upsert_jobs(conn, jobs, company_ids, location_ids, sync_run_id, result)
            if checkpoint is not None:
                checkpoint(conn)

        return result

    # ---- companies -----------------------------------------------------------

    def _upsert_companies(
        self, conn: Connection, jobs: list[PreparedJob], result: LoadResult
    ) -> dict[str, int]:
        """Create missing companies and return ``external_company_id -> companies.id``."""
        unique: dict[str, PreparedJob] = {}
        for job in jobs:
            if job.company_external_id and job.company_external_id not in unique:
                unique[job.company_external_id] = job
        if not unique:
            return {}

        source = jobs[0].source

        # Resolve what already exists FIRST. Without this, a company whose website (and
        # therefore domain) is NULL misses the ON CONFLICT target and gets a fresh row on
        # every run, while company_sources DO NOTHING leaves the new row orphaned.
        mapping: dict[str, int] = {}
        for chunk in _chunks(list(unique)):
            found = conn.execute(
                text(
                    """
                    SELECT external_company_id, company_id
                      FROM company_sources
                     WHERE source = :source AND external_company_id = ANY(:ids)
                    """
                ),
                {"source": source, "ids": chunk},
            ).all()
            mapping.update({row.external_company_id: row.company_id for row in found})

        missing = {key: job for key, job in unique.items() if key not in mapping}
        if not missing:
            return mapping

        rows = [
            {
                "source": source,
                "external_company_id": external_id,
                "name": job.company_name or f"Unknown company {external_id}",
                "name_normalized": (job.company_name or "").strip().lower()
                or f"unknown-{external_id}",
                "website": job.company_website,
                "domain": _domain_of(job.company_website),
                "ats": job.ats_provider,
                "career_url": job.company_career_url,
                "industry": job.company_industry,
                "size_range": job.company_size,
            }
            for external_id, job in missing.items()
        ]

        for chunk in _chunks(rows):
            # One statement per company: the CTE creates (or reuses) the company row and
            # binds it to this source atomically, so the two tables cannot drift apart.
            conn.execute(
                text(
                    """
                    WITH ins AS (
                        INSERT INTO companies
                            (name, name_normalized, website, domain, ats, career_url,
                             industry, size_range)
                        VALUES (:name, :name_normalized, :website, :domain, :ats,
                                :career_url, :industry, :size_range)
                        ON CONFLICT (domain) WHERE domain IS NOT NULL
                        DO UPDATE SET
                            name = EXCLUDED.name,
                            career_url = COALESCE(EXCLUDED.career_url, companies.career_url),
                            industry = COALESCE(EXCLUDED.industry, companies.industry),
                            size_range = COALESCE(EXCLUDED.size_range, companies.size_range)
                        RETURNING id
                    )
                    INSERT INTO company_sources (company_id, source, external_company_id)
                    SELECT id, :source, :external_company_id FROM ins
                    ON CONFLICT (source, external_company_id) DO NOTHING
                    """
                ),
                chunk,
            )

        for chunk in _chunks(list(missing)):
            found = conn.execute(
                text(
                    """
                    SELECT external_company_id, company_id
                      FROM company_sources
                     WHERE source = :source AND external_company_id = ANY(:ids)
                    """
                ),
                {"source": source, "ids": chunk},
            ).all()
            mapping.update({row.external_company_id: row.company_id for row in found})

        result.companies_created += len(missing)
        return mapping

    # ---- locations -----------------------------------------------------------

    def _upsert_locations(
        self, conn: Connection, jobs: list[PreparedJob], result: LoadResult
    ) -> dict[tuple, int]:
        """Create missing location rows and return their ids keyed by the natural key."""
        unique: dict[tuple, dict[str, Any]] = {}
        for job in jobs:
            if not job.country_code:
                continue
            key = (
                job.country_code,
                job.state_code or "",
                job.city_normalized or "",
                job.postal_code or "",
            )
            if key not in unique:
                unique[key] = {
                    "country_code": job.country_code,
                    "state_code": job.state_code,
                    "city": job.city,
                    "city_normalized": job.city_normalized,
                    "postal_code": job.postal_code,
                }
        if not unique:
            return {}

        for chunk in _chunks(list(unique.values())):
            conn.execute(
                text(
                    """
                    INSERT INTO job_locations
                        (country_code, state_code, city, city_normalized, postal_code)
                    VALUES (:country_code, :state_code, :city, :city_normalized, :postal_code)
                    ON CONFLICT (country_code,
                                 COALESCE(state_code, ''),
                                 COALESCE(city_normalized, ''),
                                 COALESCE(postal_code, ''))
                    DO NOTHING
                    """
                ),
                chunk,
            )

        mapping: dict[tuple, int] = {}
        for chunk in _chunks(list(unique.values())):
            found = conn.execute(
                text(
                    """
                    SELECT id, country_code,
                           COALESCE(state_code, '') AS state_code,
                           COALESCE(city_normalized, '') AS city_normalized,
                           COALESCE(postal_code, '') AS postal_code
                      FROM job_locations
                     WHERE country_code = ANY(:countries)
                       AND COALESCE(city_normalized, '') = ANY(:cities)
                    """
                ),
                {
                    "countries": list({row["country_code"] for row in chunk}),
                    "cities": list({row["city_normalized"] or "" for row in chunk}),
                },
            ).all()
            for row in found:
                mapping[
                    (row.country_code, row.state_code, row.city_normalized, row.postal_code)
                ] = row.id

        result.locations_created += len(mapping)
        return mapping

    # ---- jobs ----------------------------------------------------------------

    def _upsert_jobs(
        self,
        conn: Connection,
        jobs: list[PreparedJob],
        company_ids: dict[str, int],
        location_ids: dict[tuple, int],
        sync_run_id: int | None,
        result: LoadResult,
    ) -> None:
        source = jobs[0].source
        now = utc_now()

        # --- L1: which of these source rows do we already know about? ---------
        existing: dict[str, dict[str, Any]] = {}
        external_ids = [job.external_id for job in jobs]
        for chunk in _chunks(external_ids):
            rows = conn.execute(
                text(
                    """
                    SELECT js.external_job_id, js.job_id, j.content_hash, j.status
                      FROM job_sources js
                      JOIN jobs j ON j.id = js.job_id
                     WHERE js.source = :source AND js.external_job_id = ANY(:ids)
                    """
                ),
                {"source": source, "ids": chunk},
            ).all()
            for row in rows:
                existing[row.external_job_id] = {
                    "job_id": row.job_id,
                    "content_hash": bytes(row.content_hash),
                    "status": row.status,
                }

        to_insert = [job for job in jobs if job.external_id not in existing]
        to_update = [job for job in jobs if job.external_id in existing]

        # --- L2/L4: do any new source rows describe a job we already hold? ----
        merge_targets = self._find_merge_targets(conn, to_insert)

        # --- inserts ----------------------------------------------------------
        fresh = [job for job in to_insert if job.external_id not in merge_targets]
        merges = [job for job in to_insert if job.external_id in merge_targets]

        for chunk in _chunks(fresh):
            # Each new job carries a client-generated canonical_job_id.
            #
            # This is what makes provenance reliable. `RETURNING id` does not survive an
            # executemany — the driver discards the result sets — and even with a
            # multi-VALUES insert, correlating returned ids back to input rows by position
            # is not a guarantee PostgreSQL makes. Generating the key here means the
            # follow-up inserts JOIN on a column we already know, with no ordering
            # assumption at all.
            keys = {job.external_id: str(uuid4()) for job in chunk}
            params = []
            for job in chunk:
                payload = self._job_params(job, company_ids, location_ids, now)
                payload["canonical_job_id"] = keys[job.external_id]
                params.append(payload)

            conn.execute(
                text(
                    """
                    INSERT INTO jobs (
                        canonical_job_id,
                        title, title_normalized, description_text, description_html,
                        company_id, location_id, country_code, state_code, city,
                        remote_type, employment_type, seniority, department,
                        salary_min, salary_max, salary_currency, salary_interval,
                        posted_at, posted_at_is_valid, first_seen_at, last_seen_at,
                        source_fetched_at, close_at, closed_at, status,
                        job_url, apply_url, apply_url_canonical, apply_url_hash,
                        source, content_hash, dedupe_fingerprint,
                        category_slug, seniority_level, industry
                    ) VALUES (
                        CAST(:canonical_job_id AS uuid),
                        :title, :title_normalized, :description_text, :description_html,
                        :company_id, :location_id, :country_code, :state_code, :city,
                        CAST(:remote_type AS remote_type),
                        CAST(:employment_type AS employment_type),
                        :seniority, :department,
                        :salary_min, :salary_max, :salary_currency,
                        CAST(:salary_interval AS salary_interval),
                        :posted_at, :posted_at_is_valid, :now, :now,
                        :source_fetched_at, :close_at, :closed_at,
                        CAST(:status AS job_status),
                        :job_url, :apply_url, :apply_url_canonical, :apply_url_hash,
                        :source, :content_hash, :dedupe_fingerprint,
                        :category_slug, CAST(:seniority_level AS seniority_level), :industry
                    )
                    ON CONFLICT (canonical_job_id) DO NOTHING
                    """
                ),
                params,
            )

            # Provenance, joined on the key we generated. This is the row that makes
            # ingestion idempotent: without it, a re-run would insert every job again.
            conn.execute(
                text(
                    """
                    INSERT INTO job_sources
                        (job_id, source, external_job_id, source_apply_url,
                         ats_provider, is_primary, matched_by)
                    SELECT j.id, :source, :external_job_id, :apply_url,
                           :ats_provider, TRUE, CAST('L1_SOURCE_ID' AS dedupe_level)
                      FROM jobs j
                     WHERE j.canonical_job_id = CAST(:canonical_job_id AS uuid)
                    ON CONFLICT (source, external_job_id) DO NOTHING
                    """
                ),
                [
                    {
                        "canonical_job_id": keys[job.external_id],
                        "source": job.source,
                        "external_job_id": job.external_id,
                        "apply_url": job.apply_url,
                        "ats_provider": job.ats_provider,
                    }
                    for job in chunk
                ],
            )

            conn.execute(
                text(
                    """
                    INSERT INTO job_events (job_id, event_type, sync_run_id, new_status)
                    SELECT j.id, CAST('CREATED' AS job_event_type), :sync_run_id,
                           CAST('ACTIVE' AS job_status)
                      FROM jobs j
                     WHERE j.canonical_job_id = CAST(:canonical_job_id AS uuid)
                    """
                ),
                [
                    {"canonical_job_id": keys[job.external_id], "sync_run_id": sync_run_id}
                    for job in chunk
                ],
            )
            result.inserted += len(chunk)

        # --- merges: attach provenance to an existing job ---------------------
        for chunk in _chunks(merges):
            conn.execute(
                text(
                    """
                    INSERT INTO job_sources
                        (job_id, source, external_job_id, source_apply_url,
                         ats_provider, is_primary, matched_by)
                    VALUES (:job_id, :source, :external_job_id, :apply_url,
                            :ats_provider, FALSE, CAST(:matched_by AS dedupe_level))
                    ON CONFLICT (source, external_job_id) DO NOTHING
                    """
                ),
                [
                    {
                        "job_id": merge_targets[job.external_id][0],
                        "source": job.source,
                        "external_job_id": job.external_id,
                        "apply_url": job.apply_url,
                        "ats_provider": job.ats_provider,
                        "matched_by": merge_targets[job.external_id][1].value,
                    }
                    for job in chunk
                ],
            )
            self._emit_events(
                conn,
                [
                    (merge_targets[job.external_id][0], JobEventType.MERGED, None, None)
                    for job in chunk
                ],
                sync_run_id,
            )
            result.merged += len(chunk)

        # --- updates ----------------------------------------------------------
        changed = [
            job
            for job in to_update
            if existing[job.external_id]["content_hash"] != job.content_hash
        ]
        unchanged = [
            job
            for job in to_update
            if existing[job.external_id]["content_hash"] == job.content_hash
        ]

        # A job we saw again but which did not change: only last_seen_at moves. This is
        # the common case (most delta rows are re-observations), so it stays a single
        # cheap UPDATE with no event.
        for chunk in _chunks(unchanged):
            conn.execute(
                text("UPDATE jobs SET last_seen_at = :now WHERE id = ANY(:ids)"),
                {"now": now, "ids": [existing[job.external_id]["job_id"] for job in chunk]},
            )
        result.unchanged += len(unchanged)

        for chunk in _chunks(changed):
            params = []
            events: list[tuple[int, JobEventType, str | None, str | None]] = []
            for job in chunk:
                job_id = existing[job.external_id]["job_id"]
                previous_status = existing[job.external_id]["status"]
                payload = self._job_params(job, company_ids, location_ids, now)
                payload["job_id"] = job_id
                params.append(payload)

                if previous_status != job.status:
                    event = (
                        JobEventType.EXPIRED
                        if job.status == JobStatus.EXPIRED.value
                        else JobEventType.REACTIVATED
                        if job.status == JobStatus.ACTIVE.value
                        else JobEventType.UPDATED
                    )
                    events.append((job_id, event, previous_status, job.status))
                else:
                    events.append((job_id, JobEventType.UPDATED, previous_status, job.status))

            conn.execute(
                text(
                    """
                    UPDATE jobs SET
                        title = :title,
                        title_normalized = :title_normalized,
                        description_text = :description_text,
                        description_html = :description_html,
                        company_id = COALESCE(:company_id, company_id),
                        location_id = COALESCE(:location_id, location_id),
                        country_code = :country_code,
                        state_code = :state_code,
                        city = :city,
                        remote_type = CAST(:remote_type AS remote_type),
                        employment_type = CAST(:employment_type AS employment_type),
                        seniority = :seniority,
                        department = :department,
                        salary_min = :salary_min,
                        salary_max = :salary_max,
                        salary_currency = :salary_currency,
                        salary_interval = CAST(:salary_interval AS salary_interval),
                        -- posted_at is source truth and is refreshed, but first_seen_at
                        -- is OUR clock and is never touched after insert.
                        posted_at = :posted_at,
                        posted_at_is_valid = :posted_at_is_valid,
                        last_seen_at = :now,
                        last_updated_at = :now,
                        source_fetched_at = :source_fetched_at,
                        close_at = :close_at,
                        closed_at = :closed_at,
                        status = CAST(:status AS job_status),
                        job_url = :job_url,
                        apply_url = :apply_url,
                        apply_url_canonical = :apply_url_canonical,
                        apply_url_hash = :apply_url_hash,
                        content_hash = :content_hash,
                        dedupe_fingerprint = :dedupe_fingerprint,
                        category_slug = :category_slug,
                        seniority_level = CAST(:seniority_level AS seniority_level),
                        industry = COALESCE(:industry, jobs.industry)
                    WHERE id = :job_id
                    """
                ),
                params,
            )
            self._emit_events(conn, events, sync_run_id)
            result.updated += len(chunk)

        # Refresh provenance timestamps for everything seen this run.
        for chunk in _chunks(external_ids):
            conn.execute(
                text(
                    """
                    UPDATE job_sources SET last_seen_at = :now
                     WHERE source = :source AND external_job_id = ANY(:ids)
                    """
                ),
                {"now": now, "source": source, "ids": chunk},
            )

    # ---- dedupe levels 2 and 4 ----------------------------------------------

    def _find_merge_targets(
        self, conn: Connection, jobs: list[PreparedJob]
    ) -> dict[str, tuple[int, DedupeLevel]]:
        """Find existing jobs that these new source rows actually describe.

        Level 2 (canonical apply URL) is tried first because it is the strongest signal
        short of the source id: the same application URL is the same job. Level 4 (content
        fingerprint) catches the case where two sources publish different URLs for
        identical content.
        """
        if not jobs:
            return {}

        targets: dict[str, tuple[int, DedupeLevel]] = {}

        # --- L2 ---------------------------------------------------------------
        by_url = {job.apply_url_hash: job for job in jobs if job.apply_url_hash}
        for chunk in _chunks(list(by_url)):
            rows = conn.execute(
                text("SELECT id, apply_url_hash FROM jobs WHERE apply_url_hash = ANY(:hashes)"),
                {"hashes": chunk},
            ).all()
            for row in rows:
                job = by_url.get(bytes(row.apply_url_hash))
                if job is not None:
                    targets[job.external_id] = (row.id, DedupeLevel.L2_APPLY_URL)

        # --- L4 ---------------------------------------------------------------
        remaining = {job.dedupe_fingerprint: job for job in jobs if job.external_id not in targets}
        for chunk in _chunks(list(remaining)):
            rows = conn.execute(
                text(
                    "SELECT id, dedupe_fingerprint FROM jobs "
                    "WHERE dedupe_fingerprint = ANY(:fingerprints)"
                ),
                {"fingerprints": chunk},
            ).all()
            for row in rows:
                job = remaining.get(bytes(row.dedupe_fingerprint))
                if job is not None:
                    targets[job.external_id] = (row.id, DedupeLevel.L4_CONTENT_FINGERPRINT)

        return targets

    # ---- events --------------------------------------------------------------

    @staticmethod
    def _emit_events(
        conn: Connection,
        events: list[tuple[int, JobEventType, Any, Any]],
        sync_run_id: int | None,
    ) -> None:
        if not events:
            return
        conn.execute(
            text(
                """
                INSERT INTO job_events
                    (job_id, event_type, source, sync_run_id, previous_status, new_status)
                VALUES (:job_id, CAST(:event_type AS job_event_type), :source, :sync_run_id,
                        CAST(:previous_status AS job_status), CAST(:new_status AS job_status))
                """
            ),
            [
                {
                    "job_id": job_id,
                    "event_type": event_type.value,
                    "source": None,
                    "sync_run_id": sync_run_id,
                    "previous_status": _enum_value(previous),
                    "new_status": _enum_value(new),
                }
                for job_id, event_type, previous, new in events
            ],
        )

    # ---- params --------------------------------------------------------------

    @staticmethod
    def _job_params(
        job: PreparedJob,
        company_ids: dict[str, int],
        location_ids: dict[tuple, int],
        now: datetime,
    ) -> dict[str, Any]:
        location_key = (
            job.country_code or "",
            job.state_code or "",
            job.city_normalized or "",
            job.postal_code or "",
        )
        return {
            "title": job.title[:500],
            "title_normalized": job.title_normalized[:500],
            "description_text": job.description_text,
            "description_html": job.description_html,
            "company_id": company_ids.get(job.company_external_id or ""),
            "location_id": location_ids.get(location_key),
            "country_code": job.country_code,
            "state_code": job.state_code,
            "city": job.city,
            "remote_type": job.remote_type,
            "employment_type": job.employment_type,
            "seniority": job.seniority,
            "department": job.department,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            "salary_currency": job.salary_currency,
            "salary_interval": job.salary_interval,
            "posted_at": job.posted_at,
            "posted_at_is_valid": job.posted_at_is_valid,
            "source_fetched_at": job.source_fetched_at,
            "close_at": job.close_at,
            "closed_at": job.closed_at,
            "status": job.status,
            "job_url": job.job_url,
            "apply_url": job.apply_url,
            "apply_url_canonical": job.apply_url_canonical,
            "apply_url_hash": job.apply_url_hash,
            "source": job.source,
            "content_hash": job.content_hash,
            "dedupe_fingerprint": job.dedupe_fingerprint,
            "category_slug": job.category_slug,
            "seniority_level": job.seniority_level,
            "industry": job.industry,
            "now": now,
        }


def _enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _domain_of(website: str | None) -> str | None:
    """Reduce a website to a bare domain for company identity."""
    if not website:
        return None
    cleaned = website.strip().lower()
    for prefix in ("https://", "http://"):
        cleaned = cleaned.removeprefix(prefix)
    cleaned = cleaned.removeprefix("www.").split("/")[0].split("?")[0].strip()
    return cleaned or None


def _chunks(items: list[Any], size: int = _CHUNK) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
