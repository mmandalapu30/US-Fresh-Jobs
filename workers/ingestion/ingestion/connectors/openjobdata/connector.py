"""OpenJobDataConnector — the first real ``SourceConnector`` implementation.

Reads the OpenJobData Hugging Face Storage Bucket. Access is anonymous fsspec over
``HfFileSystem``; the bucket is public and needs no token (verified).

Three properties this implementation is built around, all measured rather than assumed:

* **Discovery lists, it does not generate.** The publisher skips days, and the skipped
  sets differ between variants. A generated date range would silently lose data.
* **Reads are column-projected.** ``entire_json`` is never fetched, halving the daily
  transfer from ~238 MB to ~120 MB.
* **Nothing is trusted to be present.** ``posted_at`` is NULL for ~19% of rows and can be
  dated in the future; ``country`` contains empty strings and the literal ``REMOTE``.
  The connector reports what it sees and lets validation and the pipeline services decide.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any

from jobplatform_schemas import RejectionReason
from jobplatform_shared import SourceUnavailableError, get_logger, get_settings
from jobplatform_shared.time import ensure_utc

from ..base import (
    ConnectorCapabilities,
    NormalizedJob,
    RawRecord,
    SourceFile,
    ValidationResult,
)
from .schema import (
    COMPANY_COLUMNS,
    JOB_COLUMNS_FULL,
    JOB_COLUMNS_MINIMAL,
    SOURCE_NAME,
    STATUS_CLOSED,
    WORKPLACE_UNKNOWN,
    OpenJobDataPaths,
    decode_nested_json,
    parse_delta_date,
)

if TYPE_CHECKING:
    from jobplatform_shared.config import Settings

    from ...storage.base import ObjectStore

__all__ = ["OpenJobDataConnector"]

logger = get_logger(__name__)

#: Backoff step for retrying a directory listing, multiplied by the attempt number. The
#: resets this guards against arrive in short bursts, so seconds are enough; minutes would
#: only delay a scheduled run that has all day to finish.
_LIST_RETRY_BASE_SECONDS = 5

#: Row groups are small (about 850 rows each in the observed files), so reading them one at
#: a time would mean ~95 round trips per file. Batching amortises that without holding a
#: whole 120 MB file in memory.
_DEFAULT_ROW_GROUP_BATCH = 8


class OpenJobDataConnector:
    """``SourceConnector`` for the OpenJobData bucket."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        filesystem: Any | None = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._paths = OpenJobDataPaths(
            bucket_uri=self._settings.openjobdata_bucket_uri,
            variant=self._settings.openjobdata_variant,
        )
        self._fs = filesystem
        self._object_store = object_store
        self._company_cache: dict[int, dict[str, Any]] | None = None

    # ---- infrastructure ------------------------------------------------------

    @property
    def filesystem(self) -> Any:
        """Lazily construct the Hugging Face filesystem.

        Lazy so the connector can be imported and unit-tested without network access or
        the ``huggingface_hub`` package being importable at module load.
        """
        if self._fs is None:
            from huggingface_hub import HfFileSystem

            token = self._settings.huggingface_token
            self._fs = HfFileSystem(
                token=token.get_secret_value() if token else None,
            )
        return self._fs

    @cached_property
    def _columns(self) -> tuple[str, ...]:
        """The projection actually read.

        ``entire_json`` is excluded unless explicitly enabled, because it is half the file
        and this platform never reads it.
        """
        if self._settings.openjobdata_variant == "minimal":
            return JOB_COLUMNS_MINIMAL
        if self._settings.openjobdata_include_entire_json:
            return (*JOB_COLUMNS_FULL, "entire_json")
        return JOB_COLUMNS_FULL

    # ---- SourceConnector -----------------------------------------------------

    def get_source_name(self) -> str:
        return SOURCE_NAME

    def get_capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            supports_incremental=True,
            supports_full_refresh=True,
            # The source ships status='closed' with a close_time, so absence from a delta
            # is NOT evidence of removal. This drives lifecycle semantics downstream.
            reports_closures=True,
            # Same-day publication, but the hour varies (06:33-14:15 UTC observed), so
            # staleness alerting allows more than 24 h before firing.
            expected_cadence_hours=36,
        )

    def _list_with_retry(self, path: str) -> list[dict[str, Any]]:
        """List a remote directory, retrying transient failures.

        The source resets connections intermittently, and this one call had no retry
        while downloads did -- so a scheduled run could die at discovery having done
        nothing, which is exactly what happened three nights running. Honours
        ``openjobdata_retry_attempts`` so the behaviour is configurable rather than baked
        in, and backs off linearly because the resets come in short bursts.

        A missing directory is not retried: that is a settled answer, not a flaky one.
        """
        attempts = max(1, self._settings.openjobdata_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return self.filesystem.ls(path, detail=True)
            except FileNotFoundError as exc:
                raise SourceUnavailableError(
                    f"delta directory not found: {path}", source=SOURCE_NAME
                ) from exc
            except Exception as exc:
                if attempt == attempts:
                    raise SourceUnavailableError(
                        f"cannot list the source after {attempts} attempts: "
                        f"{type(exc).__name__}: {exc}",
                        source=SOURCE_NAME,
                    ) from exc
                delay = _LIST_RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "source.list.retrying",
                    source=SOURCE_NAME,
                    path=path,
                    attempt=attempt,
                    of=attempts,
                    error=type(exc).__name__,
                    retry_in_seconds=delay,
                )
                time.sleep(delay)
        # Unreachable: the loop either returns or raises on its final attempt.
        raise SourceUnavailableError(f"cannot list the source: {path}", source=SOURCE_NAME)

    def discover(self, *, since: date | None = None) -> Sequence[SourceFile]:
        """List the delta files that currently exist.

        Lists the real directory rather than generating a date range: the publisher skips
        days (verified: 3-4 gaps in a 76-day window, differing per variant), so a
        generated sequence would request files that do not exist and, worse, would never
        notice a backfilled one appearing later.
        """
        entries = self._list_with_retry(self._paths.changes_dir)

        files: list[SourceFile] = []
        skipped: list[str] = []

        for entry in entries:
            name = entry["name"].rsplit("/", 1)[-1]
            file_date = parse_delta_date(name)
            if file_date is None:
                # Not a delta file (README, partial upload). Ignore rather than fail.
                skipped.append(name)
                continue
            if since is not None and file_date < since:
                continue

            files.append(
                SourceFile(
                    remote_path=entry["name"],
                    file_date=file_date,
                    size_bytes=entry.get("size"),
                    # xet_hash is a real content hash, so a corrected republish of the
                    # same filename is detected and re-ingested.
                    etag=entry.get("xet_hash"),
                    last_modified=_coerce_datetime(entry.get("uploaded_at") or entry.get("mtime")),
                    metadata={"variant": self._settings.openjobdata_variant},
                )
            )

        files.sort(key=lambda f: f.file_date or date.min)

        if skipped:
            logger.info("openjobdata.discover.ignored_entries", names=skipped[:10])
        logger.info(
            "openjobdata.discover",
            variant=self._settings.openjobdata_variant,
            found=len(files),
            newest=files[-1].file_date.isoformat() if files else None,
            gaps=_missing_dates(files),
        )
        return files

    def fetch(self, source_file: SourceFile) -> Iterator[RawRecord]:
        """Stream records from one delta file.

        Streams by row-group batch. A single file holds ~81,000 rows, so materialising it
        would waste the memory the worker needs for Arrow buffers.
        """
        import pyarrow.parquet as pq

        row_index = 0
        try:
            # Inside the try: an unreachable or missing file must surface as the typed
            # SourceUnavailableError that the ingestion task knows how to retry, not as a
            # raw OSError from the filesystem layer.
            opened = self._open_for_read(source_file)
        except Exception as exc:
            raise SourceUnavailableError(
                f"cannot open {source_file.remote_path}: {type(exc).__name__}: {exc}",
                source=SOURCE_NAME,
                remote_path=source_file.remote_path,
            ) from exc

        try:
            parquet = pq.ParquetFile(opened)
            total_groups = parquet.metadata.num_row_groups
            batch = self._settings.ingest_row_group_batch_size or _DEFAULT_ROW_GROUP_BATCH

            logger.info(
                "openjobdata.fetch.start",
                path=source_file.remote_path,
                rows=parquet.metadata.num_rows,
                row_groups=total_groups,
                columns=len(self._columns),
            )

            for start in range(0, total_groups, batch):
                group_ids = list(range(start, min(start + batch, total_groups)))
                table = parquet.read_row_groups(group_ids, columns=list(self._columns))

                for row in table.to_pylist():
                    external_id = row.get("id")
                    if not external_id:
                        # Cannot key it, so it cannot be a RawRecord. Surfaced as a
                        # rejection by the caller via validate() on a placeholder id.
                        external_id = ""
                    yield RawRecord(
                        external_id=str(external_id),
                        payload=row,
                        source_path=source_file.remote_path,
                        row_index=row_index,
                    )
                    row_index += 1
        except Exception as exc:
            raise SourceUnavailableError(
                f"failed reading {source_file.remote_path}: {type(exc).__name__}: {exc}",
                source=SOURCE_NAME,
                remote_path=source_file.remote_path,
            ) from exc
        finally:
            opened.close()

    def fetch_incremental(self, *, since: date | None = None) -> Iterator[RawRecord]:
        """Stream every record from all delta files at or after ``since``."""
        for source_file in self.discover(since=since):
            yield from self.fetch(source_file)

    def validate(self, record: RawRecord) -> ValidationResult:
        """Structural validation only.

        Deliberately narrow: this checks the source gave us something usable. Cross-cutting
        policy — is it in the U.S., is ``posted_at`` implausible, is it a duplicate — lives
        in shared pipeline services so every source is judged by identical rules.
        """
        payload = record.payload
        reasons: list[RejectionReason] = []

        if not record.external_id:
            reasons.append(RejectionReason.MISSING_EXTERNAL_ID)

        if not (payload.get("title") or "").strip():
            reasons.append(RejectionReason.MISSING_TITLE)

        if payload.get("company_id") is None:
            reasons.append(RejectionReason.MISSING_COMPANY)

        apply_url = (payload.get("apply_url") or "").strip()
        if not apply_url:
            # ~0.14% of rows. A job with no way to apply is not useful to a job seeker.
            reasons.append(RejectionReason.MISSING_APPLY_URL)
        elif not apply_url.lower().startswith(("http://", "https://")):
            reasons.append(RejectionReason.INVALID_URL)

        # The full variant must carry a decodable job model; without it there is no
        # location, salary or description, which is the entire reason for using it.
        if (
            "job_model_json" in self._columns
            and decode_nested_json(payload.get("job_model_json")) is None
        ):
            reasons.append(RejectionReason.UNPARSEABLE_PAYLOAD)

        if reasons:
            return ValidationResult(
                is_valid=False,
                reasons=reasons,
                detail=f"row {record.row_index} of {record.source_path}",
            )
        return ValidationResult.ok()

    def normalize(self, record: RawRecord) -> NormalizedJob:
        """Map a validated row onto the source-agnostic shape.

        Location and employment values stay **raw**. A connector reports what the source
        said; deciding whether "OH" means Ohio, or whether a job is in the U.S., belongs to
        LocationNormalizer so every source is treated identically.
        """
        payload = record.payload
        model = decode_nested_json(payload.get("job_model_json")) or {}
        location = model.get("location") if isinstance(model.get("location"), dict) else {}
        compensation = (
            model.get("compensation") if isinstance(model.get("compensation"), dict) else {}
        )
        metadata = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
        company = self._lookup_company(payload.get("company_id"))

        status_raw = (payload.get("status") or "").strip().lower()

        return NormalizedJob(
            source=SOURCE_NAME,
            external_id=record.external_id,
            title=(payload.get("title") or "").strip(),
            company_name=company.get("name") if company else None,
            company_external_id=(
                str(payload["company_id"]) if payload.get("company_id") is not None else None
            ),
            description_text=_clean_text(model.get("description_plain")),
            description_html=model.get("description_html"),
            department=_clean_text(payload.get("department"))
            or _clean_text(model.get("department")),
            seniority=_clean_text(model.get("seniority")),
            # ---- location, exactly as reported
            raw_country=_clean_text(payload.get("country")) or _clean_text(location.get("country")),
            raw_state=_clean_text(location.get("state")),
            raw_city=_clean_text(location.get("city")),
            raw_postal_code=_clean_text(location.get("postal_code")),
            raw_location_text=_clean_text(location.get("raw_location_text")),
            raw_workplace_type=_normalize_workplace(payload.get("workplace_type")),
            source_is_remote=_coerce_bool(payload.get("is_remote")),
            # ---- employment and pay
            raw_employment_type=_clean_text(payload.get("employment_type")),
            salary_min=_coerce_float(compensation.get("min_amount")),
            salary_max=_coerce_float(compensation.get("max_amount")),
            salary_currency=_clean_text(compensation.get("currency")),
            raw_salary_interval=_clean_text(compensation.get("interval")),
            # ---- timestamps, kept distinct (docs/01-architecture.md §C.2)
            posted_at=ensure_utc(_coerce_datetime(payload.get("posted_at"))),
            source_fetched_at=ensure_utc(_coerce_datetime(payload.get("fetched_time"))),
            # expires_at is the EMPLOYER's stated close date (~10% coverage)...
            close_at=ensure_utc(_coerce_datetime(model.get("expires_at"))),
            # ...whereas close_time is when the SOURCE noticed it was closed. Different
            # facts, different columns.
            closed_at=(
                ensure_utc(_coerce_datetime(payload.get("close_time")))
                if status_raw == STATUS_CLOSED
                else None
            ),
            # ---- links and state
            apply_url=_clean_text(payload.get("apply_url")),
            job_url=_clean_text(model.get("apply_url")) or _clean_text(payload.get("apply_url")),
            raw_status=status_raw or None,
            ats_provider=_clean_text(model.get("ats_provider")) or _ats_from_id(record.external_id),
            source_path=record.source_path,
            extra={
                "source_job_id": payload.get("job_id"),
                # Source-provided role signals. `industry` is 90% populated on the company
                # registry and was previously read but never stored; `team` and
                # `experience_level` fill gaps where department/seniority are absent.
                "company_industry": company.get("industry") if company else None,
                "company_size": company.get("size") if company else None,
                "team": _clean_text(metadata.get("team")),
                "experience_level": _clean_text(metadata.get("experience_level")),
                "requisition_type": _clean_text(metadata.get("requisition_type")),
                "company_website": company.get("website") if company else None,
                # The company registry carries its own country. It is far more reliable
                # than the per-job country field and is used downstream to corroborate
                # US classification when the job itself has no state.
                "company_country": company.get("country") if company else None,
                "company_career_url": company.get("career_url") if company else None,
                "requirements": model.get("requirements"),
                "responsibilities": model.get("responsibilities"),
            },
        )

    # ---- companies -----------------------------------------------------------

    def _lookup_company(self, company_id: object) -> dict[str, Any] | None:
        if company_id is None:
            return None
        cache = self._load_companies()
        try:
            return cache.get(int(company_id))
        except (TypeError, ValueError):
            return None

    def _load_companies(self) -> dict[int, dict[str, Any]]:
        """Load and cache the company lookup (109k rows, ~16 MB).

        Loaded once per connector instance: a per-row remote lookup would be absurd, and
        the table is small enough to hold in memory for the life of a run.
        """
        if self._company_cache is not None:
            return self._company_cache

        import pyarrow.parquet as pq

        try:
            with self.filesystem.open(self._paths.companies_path, "rb") as handle:
                table = pq.ParquetFile(handle).read(columns=list(COMPANY_COLUMNS))
        except Exception as exc:
            # A missing company lookup degrades data quality but must not abort ingestion:
            # jobs still carry company_external_id and can be enriched later.
            logger.warning(
                "openjobdata.companies.unavailable", error=f"{type(exc).__name__}: {exc}"
            )
            self._company_cache = {}
            return self._company_cache

        cache: dict[int, dict[str, Any]] = {}
        for row in table.to_pylist():
            identifier = row.get("id")
            if identifier is None:
                continue
            cache[int(identifier)] = row

        logger.info("openjobdata.companies.loaded", count=len(cache))
        self._company_cache = cache
        return cache

    # ---- archiving -----------------------------------------------------------

    def _open_for_read(self, source_file: SourceFile) -> Any:
        """Open a delta file, preferring a local archive over the network.

        When an object store is configured, an archived copy is read instead of
        re-downloading ~120 MB. That makes reprocessing cheap and keeps "what exactly did
        the source give us that day" answerable after the publisher rewrites or deletes
        a file.
        """
        if self._object_store is not None:
            key = self.archive_key(source_file)
            if self._object_store.exists(key):
                logger.info("openjobdata.fetch.from_archive", key=key)
                return self._object_store.get(key)

        return self.filesystem.open(source_file.remote_path, "rb")

    def archive_key(self, source_file: SourceFile) -> str:
        from ...storage.base import build_object_key

        return build_object_key(
            SOURCE_NAME,
            "changes",
            source_file.remote_path.rsplit("/", 1)[-1],
            variant=self._settings.openjobdata_variant,
        )

    def archive(self, source_file: SourceFile) -> str:
        """Copy a delta file into object storage and return its key.

        Streams through a buffer rather than loading the file: these are ~120-240 MB.
        """
        if self._object_store is None:
            raise SourceUnavailableError(
                "no object store configured for archiving", source=SOURCE_NAME
            )

        key = self.archive_key(source_file)
        if self._object_store.exists(key):
            logger.info("openjobdata.archive.already_present", key=key)
            return key

        logger.info("openjobdata.archive.start", path=source_file.remote_path, key=key)
        with self.filesystem.open(source_file.remote_path, "rb") as remote:
            self._object_store.put(
                key,
                remote,
                content_type="application/vnd.apache.parquet",
                metadata={
                    "source": SOURCE_NAME,
                    "variant": self._settings.openjobdata_variant,
                    "remote_path": source_file.remote_path,
                    "file_date": source_file.file_date.isoformat() if source_file.file_date else "",
                    "etag": source_file.etag or "",
                },
            )
        logger.info("openjobdata.archive.done", key=key)
        return key


# --------------------------------------------------------------------------- helpers


def _clean_text(value: object) -> str | None:
    """Strip whitespace and map empty/placeholder values to ``None``.

    The source emits empty strings for absent data (3,331 empty ``country`` values in one
    file), and ``raw_location_text`` carries leading padding. Storing ``""`` would make
    "missing" and "empty" indistinguishable downstream.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def _coerce_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # Guard against NaN/inf reaching a NUMERIC column.
    return result if result == result and abs(result) != float("inf") else None


def _coerce_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _normalize_workplace(value: object) -> str | None:
    """Map the source's ``tbc`` placeholder to ``None``.

    ``tbc`` is ~41% of rows. Passing it through would let downstream code mistake
    "unknown" for a real workplace type.
    """
    text = _clean_text(value)
    if text is None or text.lower() == WORKPLACE_UNKNOWN:
        return None
    return text


def _ats_from_id(external_id: str) -> str | None:
    """Recover the ATS provider from the id format ``{ats}:{company_slug}/{job_id}``.

    Fallback for rows whose job model is absent; the id format is stable across all
    81,149 rows checked.
    """
    if ":" not in external_id:
        return None
    prefix = external_id.split(":", 1)[0].strip()
    return prefix or None


def _missing_dates(files: Sequence[SourceFile]) -> list[str]:
    """Gaps in the discovered range, logged so a publisher outage is visible."""
    from datetime import timedelta

    dated = [f.file_date for f in files if f.file_date]
    if len(dated) < 2:
        return []
    present = set(dated)
    first, last = min(dated), max(dated)
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
        if (first + timedelta(days=offset)) not in present
    ]
