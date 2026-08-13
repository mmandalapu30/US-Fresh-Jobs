"""Domain enumerations.

These names are the contract between the database (native PG enums), the ingestion
pipeline, the API and the frontend. The stored values are the ``.value`` strings, so
renaming a member is a migration, not a refactor.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AlertChannel",
    "AlertFrequency",
    "DedupeLevel",
    "EmploymentType",
    "FreshnessBucket",
    "JobEventType",
    "JobStatus",
    "RejectionReason",
    "RemoteType",
    "SalaryInterval",
    "SyncStatus",
    "SyncTrigger",
    "UserRole",
]


class JobStatus(StrEnum):
    """Lifecycle state. Jobs are never deleted; they transition."""

    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"  # source explicitly reported it closed
    REMOVED = "REMOVED"  # vanished from the source without an explicit close
    UNKNOWN = "UNKNOWN"  # cannot be determined (e.g. source outage)


class RemoteType(StrEnum):
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    ONSITE = "ONSITE"
    UNKNOWN = "UNKNOWN"  # source ships a literal 'tbc' for ~41% of rows


class EmploymentType(StrEnum):
    FULL_TIME = "FULL_TIME"
    PART_TIME = "PART_TIME"
    CONTRACT = "CONTRACT"
    TEMPORARY = "TEMPORARY"
    INTERNSHIP = "INTERNSHIP"
    VOLUNTEER = "VOLUNTEER"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class SalaryInterval(StrEnum):
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"
    UNKNOWN = "UNKNOWN"


class JobEventType(StrEnum):
    """Append-only audit of everything that ever happened to a job."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    REPOSTED = "REPOSTED"
    EXPIRED = "EXPIRED"
    REMOVED = "REMOVED"
    REACTIVATED = "REACTIVATED"
    MERGED = "MERGED"  # deduplicated into a canonical job
    QUARANTINED = "QUARANTINED"  # failed a data-quality gate after being stored


class FreshnessBucket(StrEnum):
    """Derived at query time, never stored -- storing it would go stale every minute."""

    NEW_LAST_HOUR = "NEW_LAST_HOUR"
    NEW_LAST_6_HOURS = "NEW_LAST_6_HOURS"
    NEW_TODAY = "NEW_TODAY"
    POSTED_LAST_24_HOURS = "POSTED_LAST_24_HOURS"
    POSTED_TODAY = "POSTED_TODAY"
    UPDATED_TODAY = "UPDATED_TODAY"
    OLDER = "OLDER"
    EXPIRED = "EXPIRED"


class SyncStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"  # some files succeeded, some failed
    CANCELLED = "CANCELLED"


class SyncTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"
    BACKFILL = "BACKFILL"
    RETRY = "RETRY"


class RejectionReason(StrEnum):
    """Why a source row did not become a job.

    Rejections are stored, never silently dropped -- the spec requires it and it is the
    only way to tell a source regression from a normal day.
    """

    MISSING_TITLE = "MISSING_TITLE"
    MISSING_COMPANY = "MISSING_COMPANY"
    MISSING_LOCATION = "MISSING_LOCATION"
    MISSING_APPLY_URL = "MISSING_APPLY_URL"
    MISSING_EXTERNAL_ID = "MISSING_EXTERNAL_ID"
    INVALID_URL = "INVALID_URL"
    INVALID_DATE = "INVALID_DATE"
    FUTURE_POSTED_AT = "FUTURE_POSTED_AT"
    IMPLAUSIBLE_POSTED_AT = "IMPLAUSIBLE_POSTED_AT"
    INVALID_COUNTRY = "INVALID_COUNTRY"
    COUNTRY_NOT_ALLOWED = "COUNTRY_NOT_ALLOWED"
    #: Job is real and valid, but its role category is outside the configured
    #: ingestion scope. Recorded rather than dropped so the cost of a narrow scope
    #: stays visible.
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    #: Posted (or first detected) longer ago than the configured freshness window.
    #: A valid job, simply older than this deployment chooses to carry.
    TOO_OLD = "TOO_OLD"
    UNPARSEABLE_PAYLOAD = "UNPARSEABLE_PAYLOAD"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    DUPLICATE_IN_BATCH = "DUPLICATE_IN_BATCH"


class DedupeLevel(StrEnum):
    """Which rule matched. Recorded so merges are explainable and reversible."""

    L1_SOURCE_ID = "L1_SOURCE_ID"
    L2_APPLY_URL = "L2_APPLY_URL"
    L3_COMPANY_TITLE_LOCATION = "L3_COMPANY_TITLE_LOCATION"
    L4_CONTENT_FINGERPRINT = "L4_CONTENT_FINGERPRINT"
    NONE = "NONE"


class AlertFrequency(StrEnum):
    IMMEDIATE = "IMMEDIATE"
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"


class AlertChannel(StrEnum):
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    SMS = "SMS"


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"
    SERVICE = "SERVICE"
