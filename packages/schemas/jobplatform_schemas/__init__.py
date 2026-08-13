"""Shared Pydantic contracts, enums and reference data.

Imported by both the API and the ingestion workers so the two sides cannot drift.
"""

from .enums import (
    AlertChannel,
    AlertFrequency,
    DedupeLevel,
    EmploymentType,
    FreshnessBucket,
    JobEventType,
    JobStatus,
    RejectionReason,
    RemoteType,
    SalaryInterval,
    SyncStatus,
    SyncTrigger,
    UserRole,
)
from .pagination import Cursor, CursorPage, PageMeta, decode_cursor, encode_cursor
from .us_states import (
    STATE_CODE_TO_NAME,
    STATE_NAME_TO_CODE,
    US_STATE_CODES,
    US_STATES,
    US_TERRITORIES,
    is_valid_state_code,
    resolve_country_code,
    resolve_state_code,
)

__all__ = [
    "STATE_CODE_TO_NAME",
    "STATE_NAME_TO_CODE",
    "US_STATES",
    "US_STATE_CODES",
    "US_TERRITORIES",
    "AlertChannel",
    "AlertFrequency",
    "Cursor",
    "CursorPage",
    "DedupeLevel",
    "EmploymentType",
    "FreshnessBucket",
    "JobEventType",
    "JobStatus",
    "PageMeta",
    "RejectionReason",
    "RemoteType",
    "SalaryInterval",
    "SyncStatus",
    "SyncTrigger",
    "UserRole",
    "decode_cursor",
    "encode_cursor",
    "is_valid_state_code",
    "resolve_country_code",
    "resolve_state_code",
]
