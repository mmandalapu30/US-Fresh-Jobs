"""The ingestion pipeline: connector output in, database rows out.

Stages, in order, exactly as the architecture document describes:

    fetch -> validate -> normalize -> locate -> freshness -> dedupe -> lifecycle -> load

The pipeline is **source-agnostic**. It takes a ``SourceConnector`` and never asks which
one it is, so adding Greenhouse later changes nothing here.

Two invariants it exists to uphold:

* **Nothing is silently discarded.** Every record that does not become a job is written to
  ``sync_errors`` with a reason from a closed enum.
* **A crash cannot corrupt or lose data.** Batches are written in one transaction together
  with their checkpoint, so a partially-processed file resumes cleanly.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.engine import Engine

from jobplatform_schemas import (
    EmploymentType,
    JobStatus,
    RejectionReason,
    SalaryInterval,
    SyncStatus,
    SyncTrigger,
)
from jobplatform_shared import get_logger, get_settings
from jobplatform_shared.config import Settings
from jobplatform_shared.time import utc_now

from ..connectors.base import NormalizedJob, RawRecord, SourceConnector, SourceFile
from ..repositories.jobs import JobLoader, LoadResult, PreparedJob
from ..repositories.sync import RejectedRecord, SyncRepository, SyncRunActiveError, _RunCounters
from ..services.classify import JobClassifier
from ..services.dedupe import DedupeService
from ..services.freshness import FreshnessService
from ..services.location import LocationNormalizer

__all__ = ["IngestionPipeline", "PipelineReport"]

logger = get_logger(__name__)


@dataclass(slots=True)
class PipelineReport:
    """Outcome of a run, mirrored into ``sync_runs``."""

    sync_run_id: int | None = None
    files_discovered: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    rows_processed: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_merged: int = 0
    status: SyncStatus = SyncStatus.PENDING


#: Source employment strings are wildly inconsistent -- "Full time", "Full-time",
#: "Full Time", "FULL_TIME" all occur. Normalised by squashing separators.
_EMPLOYMENT_MAP = {
    "fulltime": EmploymentType.FULL_TIME,
    "full": EmploymentType.FULL_TIME,
    "permanent": EmploymentType.FULL_TIME,
    "regular": EmploymentType.FULL_TIME,
    "parttime": EmploymentType.PART_TIME,
    "part": EmploymentType.PART_TIME,
    "contract": EmploymentType.CONTRACT,
    "contractor": EmploymentType.CONTRACT,
    "contracttemporary": EmploymentType.CONTRACT,
    "freelance": EmploymentType.CONTRACT,
    "temporary": EmploymentType.TEMPORARY,
    "temp": EmploymentType.TEMPORARY,
    "seasonal": EmploymentType.TEMPORARY,
    "intern": EmploymentType.INTERNSHIP,
    "internship": EmploymentType.INTERNSHIP,
    "apprenticeship": EmploymentType.INTERNSHIP,
    "volunteer": EmploymentType.VOLUNTEER,
}

_INTERVAL_MAP = {
    "hourly": SalaryInterval.HOURLY,
    "hour": SalaryInterval.HOURLY,
    "daily": SalaryInterval.DAILY,
    "day": SalaryInterval.DAILY,
    "weekly": SalaryInterval.WEEKLY,
    "week": SalaryInterval.WEEKLY,
    "monthly": SalaryInterval.MONTHLY,
    "month": SalaryInterval.MONTHLY,
    "annually": SalaryInterval.ANNUAL,
    "annual": SalaryInterval.ANNUAL,
    "yearly": SalaryInterval.ANNUAL,
    "year": SalaryInterval.ANNUAL,
}


class IngestionPipeline:
    """Runs a connector end to end and writes the results."""

    def __init__(
        self,
        connector: SourceConnector,
        engine: Engine,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._connector = connector
        self._settings = settings or get_settings()
        self._sync = SyncRepository(engine)
        self._loader = JobLoader(engine)
        self._location = LocationNormalizer()
        self._freshness = FreshnessService(
            max_future_hours=self._settings.ingest_max_future_posted_at_hours,
            min_year=self._settings.ingest_min_posted_at_year,
        )
        self._dedupe = DedupeService()
        self._classifier = JobClassifier()

    # ---- entry point ---------------------------------------------------------

    def run(
        self,
        *,
        since: date | None = None,
        max_files: int | None = None,
        trigger: SyncTrigger = SyncTrigger.SCHEDULED,
        reclaim_stale: bool = True,
    ) -> PipelineReport:
        """Discover, then process every unit of work that is not already done."""
        source = self._connector.get_source_name()
        report = PipelineReport()

        if reclaim_stale:
            # A worker killed by OOM leaves a RUNNING row that blocks every future run.
            self._sync.reclaim_stale_runs(source)

        try:
            run = self._sync.start_run(source, trigger=trigger)
        except SyncRunActiveError:
            logger.warning("pipeline.already_running", source=source)
            report.status = SyncStatus.CANCELLED
            return report

        report.sync_run_id = run.id
        counters = _RunCounters()

        try:
            discovered = list(self._connector.discover(since=since))
            report.files_discovered = len(discovered)
            counters.files_discovered = len(discovered)

            pending = self._filter_pending(source, discovered)
            report.files_skipped = len(discovered) - len(pending)
            if max_files is not None:
                pending = pending[-max_files:]

            logger.info(
                "pipeline.plan",
                source=source,
                discovered=len(discovered),
                pending=len(pending),
                skipped=report.files_skipped,
            )

            for source_file in pending:
                self._process_file(run.id, source_file, report, counters)
                report.files_processed += 1
                counters.files_processed += 1

            report.status = (
                SyncStatus.SUCCEEDED if counters.files_failed == 0 else SyncStatus.PARTIAL
            )
        except Exception as exc:
            logger.exception("pipeline.failed", source=source, error=type(exc).__name__)
            report.status = SyncStatus.FAILED
            raise
        finally:
            counters.rows_processed = report.rows_processed
            counters.rows_accepted = report.rows_accepted
            counters.rows_rejected = report.rows_rejected
            counters.rows_inserted = report.rows_inserted
            counters.rows_updated = report.rows_updated
            counters.duplicates_found = report.rows_merged
            self._sync.finish_run(run.id, status=report.status, counters=counters)

        return report

    # ---- per file ------------------------------------------------------------

    def _filter_pending(self, source: str, files: list[SourceFile]) -> list[SourceFile]:
        """Drop units already completed at this exact version."""
        candidates = [(f.remote_path, f.etag) for f in files]
        remaining = set(self._sync.filter_unprocessed(source, candidates))
        return [f for f in files if (f.remote_path, f.etag or "") in remaining]

    def _process_file(
        self,
        run_id: int,
        source_file: SourceFile,
        report: PipelineReport,
        counters: _RunCounters,
    ) -> None:
        source = self._connector.get_source_name()
        record = self._sync.start_file(
            run_id,
            source,
            remote_path=source_file.remote_path,
            remote_size_bytes=source_file.size_bytes,
            remote_etag=source_file.etag,
            file_date=source_file.file_date,
        )

        committed = 0
        try:
            with self._sync.rejection_buffer(run_id, source) as rejections:
                for batch in self._batches(source_file, record.id, rejections, report):
                    if not batch:
                        continue
                    committed += len(batch)
                    rows = committed

                    def checkpoint(conn, rows=rows) -> None:
                        # Same transaction as the data: a rollback undoes both, so the
                        # checkpoint can never claim rows that were not written.
                        self._sync.checkpoint_file(
                            record.id, row_groups_done=0, rows_committed=rows, conn=conn
                        )

                    result = self._loader.load(batch, sync_run_id=run_id, checkpoint=checkpoint)
                    self._apply(result, report)

            self._sync.finish_file(record.id, status=SyncStatus.SUCCEEDED, rows_committed=committed)
        except Exception as exc:
            counters.files_failed += 1
            self._sync.finish_file(
                record.id,
                status=SyncStatus.FAILED,
                rows_committed=committed,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            raise

    def _batches(
        self,
        source_file: SourceFile,
        sync_file_id: int,
        rejections,
        report: PipelineReport,
    ) -> Iterator[list[PreparedJob]]:
        """Stream records through the pipeline, yielding write-sized batches."""
        size = self._settings.ingest_upsert_chunk_size
        batch: list[PreparedJob] = []
        seen_in_batch: set[str] = set()

        for record in self._connector.fetch(source_file):
            report.rows_processed += 1

            prepared = self._prepare(record, sync_file_id, rejections, report)
            if prepared is None:
                continue

            # A file can repeat an id within itself; writing both would violate the
            # provenance UNIQUE and abort the whole batch.
            if prepared.external_id in seen_in_batch:
                rejections.add(
                    RejectedRecord(
                        reason=RejectionReason.DUPLICATE_IN_BATCH,
                        external_job_id=prepared.external_id,
                        sync_file_id=sync_file_id,
                    )
                )
                report.rows_rejected += 1
                continue

            seen_in_batch.add(prepared.external_id)
            batch.append(prepared)
            report.rows_accepted += 1

            if len(batch) >= size:
                yield batch
                batch = []
                seen_in_batch = set()

        if batch:
            yield batch

    # ---- per record ----------------------------------------------------------

    def _prepare(
        self,
        record: RawRecord,
        sync_file_id: int,
        rejections,
        report: PipelineReport,
    ) -> PreparedJob | None:
        """Run one record through validate -> normalize -> locate -> freshness -> dedupe.

        Returns ``None`` when the record is rejected; the reason is always recorded.
        """
        validation = self._connector.validate(record)
        if not validation.is_valid:
            for reason in validation.reasons:
                rejections.add(
                    RejectedRecord(
                        reason=reason,
                        external_job_id=record.external_id or None,
                        error_message=validation.detail,
                        sync_file_id=sync_file_id,
                    )
                )
            report.rows_rejected += 1
            return None

        job: NormalizedJob = self._connector.normalize(record)

        # --- location ---------------------------------------------------------
        location = self._location.normalize(
            raw_country=job.raw_country,
            raw_state=job.raw_state,
            raw_city=job.raw_city,
            raw_postal_code=job.raw_postal_code,
            raw_location_text=job.raw_location_text,
            raw_workplace_type=job.raw_workplace_type,
            source_is_remote=job.source_is_remote,
            company_country=job.extra.get("company_country"),
        )

        allowlist = self._settings.ingest_country_allowlist
        if allowlist and location.country_code not in allowlist:
            rejections.add(
                RejectedRecord(
                    reason=(
                        RejectionReason.INVALID_COUNTRY
                        if location.country_code is None
                        else RejectionReason.COUNTRY_NOT_ALLOWED
                    ),
                    external_job_id=job.external_id,
                    error_message=location.reason
                    or f"country {location.country_code!r} not in {allowlist}",
                    sync_file_id=sync_file_id,
                )
            )
            report.rows_rejected += 1
            return None

        # --- role classification ----------------------------------------------
        # Runs before the scope gate below so an out-of-scope job is dropped as early as
        # possible. Source-provided signals win over the title guess where they exist.
        role = self._classifier.classify(
            job.title,
            department=job.department or job.extra.get("team"),
            source_seniority=job.seniority or job.extra.get("experience_level"),
        )

        # --- freshness --------------------------------------------------------
        assessment = self._freshness.assess(job.posted_at)
        # A future or implausible posted_at does NOT discard the job: the posting is real,
        # only its date is untrustworthy. It is stored as-is, flagged invalid, and kept out
        # of every "recently posted" surface. The reason is still recorded so the data
        # quality signal is visible.
        if assessment.rejection_reason is not None:
            rejections.add(
                RejectedRecord(
                    reason=assessment.rejection_reason,
                    external_job_id=job.external_id,
                    error_message=assessment.detail,
                    sync_file_id=sync_file_id,
                )
            )

        # --- freshness window -------------------------------------------------
        # Applied before dedupe hashing and the write, like the role gate. The source's
        # deltas are mostly re-observations of old postings, so without this the board
        # fills with months-old jobs while genuinely new ones are buried.
        max_age = self._settings.ingest_max_posted_age_days
        if max_age > 0:
            cutoff = utc_now() - timedelta(days=max_age)
            # Prefer the employer's date. When absent, our own detection time is the
            # only honest signal -- and dropping those outright would discard 17% of
            # the feed for a fault that is the source's, not the job's.
            reference = assessment.posted_at if assessment.is_valid else None
            if reference is None and self._settings.ingest_age_fallback_to_first_seen:
                reference = utc_now()  # first seen right now, by definition

            if reference is None or reference < cutoff:
                rejections.add(
                    RejectedRecord(
                        reason=RejectionReason.TOO_OLD,
                        external_job_id=job.external_id,
                        error_message=(
                            f"posted {reference.date()} — older than the {max_age}-day window"
                            if reference
                            else f"no usable date and no fallback; window is {max_age} days"
                        ),
                        sync_file_id=sync_file_id,
                    )
                )
                report.rows_rejected += 1
                return None

        # --- role scope -------------------------------------------------------
        # Applied here, BEFORE dedupe hashing and the database write, so an out-of-scope
        # job costs only the classification that already happened. Doing it after the
        # load would mean paying for storage and index maintenance on rows nobody wants.
        #
        # An empty allowlist means "everything", which is the default: the platform's
        # stated principle is to keep all qualifying jobs. Narrowing is opt-in.
        allowed = self._settings.ingest_category_allowlist
        blocked = self._settings.ingest_category_blocklist
        if (allowed and role.category_slug not in allowed) or (role.category_slug in blocked):
            rejections.add(
                RejectedRecord(
                    reason=RejectionReason.CATEGORY_NOT_ALLOWED,
                    external_job_id=job.external_id,
                    error_message=(
                        f"category {role.category_slug!r} is outside the configured ingestion scope"
                    ),
                    sync_file_id=sync_file_id,
                )
            )
            report.rows_rejected += 1
            return None

        # --- lifecycle --------------------------------------------------------
        status = (
            JobStatus.EXPIRED if (job.raw_status or "").lower() == "closed" else JobStatus.ACTIVE
        )
        # The schema requires a closure timestamp for EXPIRED; fall back to the fetch time
        # when the source reported closure without one.
        closed_at = job.closed_at
        if status is JobStatus.EXPIRED and closed_at is None and job.close_at is None:
            closed_at = job.source_fetched_at

        # --- dedupe -----------------------------------------------------------
        employment = _map_employment(job.raw_employment_type)
        interval = _map_interval(job.raw_salary_interval)

        # The source ships transposed ranges (observed min=85000 with max=60000). A range
        # is a range, so the pair is ordered rather than discarded -- rejecting the row
        # would lose a real job over a field the user can still read correctly.
        salary_min, salary_max = _order_salary(job.salary_min, job.salary_max)
        currency = _valid_currency(job.salary_currency)

        keys = self._dedupe.compute(
            source=job.source,
            external_id=job.external_id,
            title=job.title,
            company_name=job.company_name,
            company_external_id=job.company_external_id,
            apply_url=job.apply_url,
            country_code=location.country_code,
            state_code=location.state_code,
            city=location.city,
            description_text=job.description_text,
            salary_min=salary_min,
            salary_max=salary_max,
            remote_type=location.remote_type.value,
            employment_type=employment.value,
        )

        return PreparedJob(
            source=job.source,
            external_id=job.external_id,
            title=job.title,
            title_normalized=self._dedupe.normalize_title(job.title) or job.title.lower(),
            company_name=job.company_name,
            company_external_id=job.company_external_id,
            country_code=location.country_code,
            state_code=location.state_code,
            city=location.city,
            city_normalized=location.city_normalized,
            postal_code=location.postal_code,
            remote_type=location.remote_type.value,
            employment_type=employment.value,
            seniority=job.seniority,
            department=job.department,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            salary_interval=interval.value,
            posted_at=assessment.posted_at,
            posted_at_is_valid=assessment.is_valid,
            source_fetched_at=job.source_fetched_at,
            close_at=job.close_at,
            closed_at=closed_at,
            status=status.value,
            apply_url=job.apply_url,
            apply_url_canonical=keys.canonical_apply_url,
            apply_url_hash=keys.apply_url_hash,
            job_url=job.job_url,
            content_hash=keys.content_hash,
            dedupe_fingerprint=keys.content_fingerprint,
            description_text=job.description_text,
            description_html=job.description_html,
            ats_provider=job.ats_provider,
            category_slug=role.category_slug,
            seniority_level=role.seniority_level,
            company_website=job.extra.get("company_website"),
            company_industry=_normalize_industry(job.extra.get("company_industry")),
            company_size=job.extra.get("company_size"),
            industry=_normalize_industry(job.extra.get("company_industry")),
            company_career_url=job.extra.get("company_career_url"),
        )

    @staticmethod
    def _apply(result: LoadResult, report: PipelineReport) -> None:
        report.rows_inserted += result.inserted
        report.rows_updated += result.updated
        report.rows_unchanged += result.unchanged
        report.rows_merged += result.merged


def _normalize_industry(value: str | None) -> str | None:
    """Title-case the source's lowercased industry strings.

    The company registry stores them lowercased ("hospital & health care"), which reads
    badly as a filter label. Normalising once at ingestion means neither the API nor the
    UI has to.
    """
    if not value:
        return None
    cleaned = " ".join(value.split()).strip()
    if not cleaned:
        return None
    # Preserve short connectives in lower case so "Oil & Gas" does not become "Oil & And".
    minor = {"and", "or", "of", "the", "for", "in", "on", "to", "a", "an"}

    def _title_word(word: str, *, first: bool) -> str:
        if word == "&":
            return word
        # Capitalise each part of a compound: "transportation/trucking/railroad" must
        # become "Transportation/Trucking/Railroad", not "Transportation/trucking/...".
        parts = re.split(r"([/\-])", word)
        rebuilt = "".join(
            part
            if part in {"/", "-"}
            else (part if (not first and part in minor) else part.capitalize())
            for part in parts
        )
        return rebuilt

    words = cleaned.split(" ")
    titled = [_title_word(word, first=(i == 0)) for i, word in enumerate(words)]
    return " ".join(titled)[:120]


def _order_salary(low: float | None, high: float | None) -> tuple[float | None, float | None]:
    """Return the pair in ascending order, dropping implausible values.

    The database enforces ``salary_min <= salary_max``; the source does not. Ordering the
    pair keeps the job rather than failing the whole batch on a transposed range.
    Negative or absurd figures are dropped instead of stored, because a salary filter is
    only useful if the numbers in it are believable.
    """
    values = [v for v in (low, high) if v is not None and 0 <= v <= 100_000_000]
    if not values:
        return None, None
    if len(values) == 1:
        return values[0], None
    return min(values), max(values)


def _valid_currency(code: str | None) -> str | None:
    """Accept only a well-formed ISO-4217 alphabetic code.

    The column has a ``^[A-Z]{3}$`` CHECK, so anything else must become NULL rather than
    abort the batch.
    """
    if not code:
        return None
    candidate = code.strip().upper()
    return candidate if len(candidate) == 3 and candidate.isalpha() else None


def _map_employment(raw: str | None) -> EmploymentType:
    if not raw:
        return EmploymentType.UNKNOWN
    key = "".join(ch for ch in raw.lower() if ch.isalnum())
    for candidate, mapped in _EMPLOYMENT_MAP.items():
        if key.startswith(candidate):
            return mapped
    return EmploymentType.OTHER


def _map_interval(raw: str | None) -> SalaryInterval:
    if not raw:
        return SalaryInterval.UNKNOWN
    return _INTERVAL_MAP.get(raw.strip().lower(), SalaryInterval.UNKNOWN)
