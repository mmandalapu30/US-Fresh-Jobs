"""Source connector interface.

Every job source — OpenJobData now; Greenhouse, Ashby, Lever, Jobven and employer career
sites later — is reached only through this Protocol. The pipeline, the repositories and
the API know about ``SourceConnector``; none of them knows that OpenJobData exists.

``scripts/check_layering.py`` enforces that mechanically: the strings ``openjobdata``,
``huggingface`` and ``hf://`` may appear only inside
``ingestion/connectors/openjobdata/`` and its tests. CI fails the build otherwise.

The method set matches the conceptual interface in the specification:

    discover() · fetch() · fetch_incremental() · normalize() · validate() · get_source_name()
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from jobplatform_schemas import RejectionReason

__all__ = [
    "ConnectorCapabilities",
    "NormalizedJob",
    "RawRecord",
    "SourceConnector",
    "SourceFile",
    "ValidationResult",
]


@dataclass(frozen=True, slots=True)
class SourceFile:
    """One discoverable unit of work at a source.

    For a bucket-backed source this is a file; for an HTTP API it may be a page or a
    cursor window. The abstraction is "something that can be fetched, checkpointed and
    re-fetched idempotently".
    """

    #: Stable identifier at the source. Used as the checkpoint key.
    remote_path: str
    #: Logical date the unit covers, when the source is date-partitioned. May be ``None``
    #: for sources that are not.
    file_date: date | None = None
    size_bytes: int | None = None
    #: Version marker. Any change means "re-ingest": the source may correct a file in
    #: place, and we must not skip a corrected version because the path already exists.
    etag: str | None = None
    last_modified: datetime | None = None
    #: Free-form connector context (variant, partition, page token, ...). Never
    #: interpreted outside the connector that produced it.
    #: Excluded from equality and hashing: a unit's identity is its path plus version,
    #: and a dict field would otherwise make this frozen dataclass unhashable.
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def checkpoint_key(self) -> tuple[str, str]:
        """``(remote_path, version)`` — the identity used for resumability.

        Including the version means a re-published file is treated as new work while a
        byte-identical one is skipped.
        """
        return self.remote_path, self.etag or (str(self.size_bytes) if self.size_bytes else "")


@dataclass(slots=True)
class RawRecord:
    """One untransformed record as the source provided it.

    Kept deliberately close to the wire so that ``sync_errors`` can store exactly what
    arrived when a row is rejected.
    """

    #: The source's own primary key for this record.
    external_id: str
    payload: dict[str, Any]
    #: Which ``SourceFile`` it came from, for traceability.
    source_path: str | None = None
    #: Ordinal within the file, so a rejection can be located precisely.
    row_index: int | None = None


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating one record.

    A rejection always carries a machine-readable reason. Nothing is ever silently
    dropped — the spec requires it, and it is the only way to distinguish a source
    regression from a normal day.
    """

    is_valid: bool
    reasons: list[RejectionReason] = field(default_factory=list)
    detail: str | None = None

    @classmethod
    def ok(cls) -> ValidationResult:
        return cls(is_valid=True)

    @classmethod
    def reject(cls, *reasons: RejectionReason, detail: str | None = None) -> ValidationResult:
        if not reasons:
            raise ValueError("a rejection must state at least one reason")
        return cls(is_valid=False, reasons=list(reasons), detail=detail)


@dataclass(slots=True)
class NormalizedJob:
    """Source-agnostic job record produced by ``normalize()``.

    This is the boundary: everything upstream is the connector's problem, everything
    downstream (location classification, freshness, dedupe, loading) operates on this
    shape regardless of which source produced it.

    Location and freshness fields are populated by pipeline services in later milestones,
    not by the connector — a connector reports what the source said, it does not decide
    whether a job is in the U.S.
    """

    # ---- identity
    source: str
    external_id: str

    # ---- core content
    title: str
    company_name: str | None = None
    company_external_id: str | None = None
    description_text: str | None = None
    description_html: str | None = None
    department: str | None = None
    seniority: str | None = None

    # ---- location, exactly as the source reported it
    raw_country: str | None = None
    raw_state: str | None = None
    raw_city: str | None = None
    raw_postal_code: str | None = None
    raw_location_text: str | None = None
    raw_workplace_type: str | None = None
    source_is_remote: bool | None = None

    # ---- employment and compensation
    raw_employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    raw_salary_interval: str | None = None

    # ---- timestamps, kept distinct; see docs/01-architecture.md §C.2
    posted_at: datetime | None = None
    source_fetched_at: datetime | None = None
    close_at: datetime | None = None
    closed_at: datetime | None = None

    # ---- links and state
    apply_url: str | None = None
    job_url: str | None = None
    raw_status: str | None = None
    ats_provider: str | None = None

    # ---- provenance
    source_path: str | None = None
    skills: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    """What a connector can do, so the scheduler can treat sources differently.

    Declared rather than inferred: a source that cannot do incremental fetches must not
    be scheduled as if it can.
    """

    supports_incremental: bool
    supports_full_refresh: bool
    #: True when the source reports closures explicitly (so absence means REMOVED rather
    #: than EXPIRED). Drives lifecycle semantics.
    reports_closures: bool = False
    #: Expected publication cadence in hours; used to alert on staleness.
    expected_cadence_hours: int | None = None


@runtime_checkable
class SourceConnector(Protocol):
    """The contract every job source implements."""

    def get_source_name(self) -> str:
        """Stable identifier stored in ``jobs.source`` and ``job_sources.source``.

        Changing this value for an existing source would orphan its provenance rows, so
        it is treated as permanent once data has been ingested.
        """
        ...

    def get_capabilities(self) -> ConnectorCapabilities: ...

    def discover(self, *, since: date | None = None) -> Sequence[SourceFile]:
        """List the work currently available at the source.

        Must reflect what the source actually offers right now, by listing rather than by
        assuming a pattern. Sources skip periods, republish, and backfill; a connector
        that generates an expected sequence instead of reading the real one will silently
        lose data.
        """
        ...

    def fetch(self, source_file: SourceFile) -> Iterator[RawRecord]:
        """Stream the records of one unit of work.

        Streaming rather than returning a list: a single unit can hold ~81,000 records,
        and materialising every one before processing wastes memory the worker needs for
        Parquet buffers.
        """
        ...

    def fetch_incremental(self, *, since: date | None = None) -> Iterator[RawRecord]:
        """Stream only records that are new or changed since ``since``.

        Convenience over ``discover()`` + ``fetch()`` for sources where that is cheaper.
        Connectors without incremental support raise ``NotImplementedError`` and declare
        ``supports_incremental=False``.
        """
        ...

    def validate(self, record: RawRecord) -> ValidationResult:
        """Check one raw record against the source's own expectations.

        Structural validation only — required fields, parseable payloads. Cross-cutting
        rules (U.S. classification, future-dated timestamps) belong to pipeline services
        so every source is judged by identical standards.
        """
        ...

    def normalize(self, record: RawRecord) -> NormalizedJob:
        """Convert a validated raw record into the source-agnostic shape.

        Only called for records that passed ``validate()``.
        """
        ...
