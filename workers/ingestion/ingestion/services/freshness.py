"""FreshnessService — decide whether a posted_at can be trusted, and how fresh a job is.

Two responsibilities, deliberately separated:

1. **Validity** (`assess`) — is the source's ``posted_at`` usable at all? Decided once at
   ingestion and stored as ``jobs.posted_at_is_valid``, because the answer does not change
   over time and the hot feed index depends on it.
2. **Bucketing** (`classify`) — which freshness bucket does a job fall into *right now*?
   Derived at query time and never stored, because "posted today" goes stale every minute.

Both exist because the source cannot be taken at face value (verified):

* ``posted_at`` is **NULL for 19.3%** of rows.
* ``posted_at`` contains **real future dates** — the observed maximum was 34 days ahead.
* Values range back to **2013**, long before the platform existed.

A future-dated or absurd ``posted_at`` is never silently clamped. Clamping would fabricate
a timestamp, which the specification forbids; instead the value is stored as-is, flagged
invalid, and excluded from every "recently posted" surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from jobplatform_schemas import FreshnessBucket, JobStatus, RejectionReason
from jobplatform_shared.time import ensure_utc, utc_now

__all__ = ["FreshnessAssessment", "FreshnessService"]


@dataclass(frozen=True, slots=True)
class FreshnessAssessment:
    """Whether a job's ``posted_at`` can be trusted."""

    posted_at: datetime | None
    is_valid: bool
    #: Set when the value is unusable, so the rejection or flag is explainable.
    rejection_reason: RejectionReason | None = None
    detail: str | None = None


class FreshnessService:
    """Validity and freshness policy, applied identically to every source."""

    def __init__(
        self,
        *,
        max_future_hours: int = 24,
        min_year: int = 2000,
    ) -> None:
        #: Small tolerance rather than zero: a source publishing at 23:59 in a local
        #: timezone can legitimately look slightly ahead of our UTC clock.
        self._max_future_hours = max_future_hours
        self._min_year = min_year

    # ---- validity ------------------------------------------------------------

    def assess(
        self, posted_at: datetime | None, *, now: datetime | None = None
    ) -> FreshnessAssessment:
        """Judge a source ``posted_at``.

        A missing value is *not* an error — it is the documented behaviour of ~19% of
        rows. Those jobs are still ingested and still searchable; they simply never appear
        in a "posted recently" bucket, because inventing a date for them would be a lie.
        """
        current = ensure_utc(now) or utc_now()
        coerced = ensure_utc(posted_at)

        if coerced is None:
            return FreshnessAssessment(
                posted_at=None,
                is_valid=False,
                rejection_reason=None,  # absent, not invalid: the job is still kept
                detail="source did not provide posted_at",
            )

        if coerced > current + timedelta(hours=self._max_future_hours):
            return FreshnessAssessment(
                posted_at=coerced,
                is_valid=False,
                rejection_reason=RejectionReason.FUTURE_POSTED_AT,
                detail=(
                    f"posted_at {coerced.isoformat()} is "
                    f"{(coerced - current).days} day(s) in the future"
                ),
            )

        if coerced.year < self._min_year:
            return FreshnessAssessment(
                posted_at=coerced,
                is_valid=False,
                rejection_reason=RejectionReason.IMPLAUSIBLE_POSTED_AT,
                detail=f"posted_at {coerced.isoformat()} predates {self._min_year}",
            )

        return FreshnessAssessment(posted_at=coerced, is_valid=True)

    # ---- bucketing -----------------------------------------------------------

    def classify(
        self,
        *,
        posted_at: datetime | None,
        posted_at_is_valid: bool,
        first_seen_at: datetime,
        last_updated_at: datetime | None = None,
        status: JobStatus = JobStatus.ACTIVE,
        now: datetime | None = None,
    ) -> FreshnessBucket:
        """Bucket a job for display.

        Order matters: the most specific and most useful bucket wins. Detection buckets
        (``NEW_*``) are checked before posting buckets because a job we found in the last
        hour is the strongest freshness signal the platform can honestly offer — it is our
        own clock, not the source's.
        """
        current = ensure_utc(now) or utc_now()

        if status in {JobStatus.EXPIRED, JobStatus.REMOVED}:
            return FreshnessBucket.EXPIRED

        seen = ensure_utc(first_seen_at)
        if seen is not None:
            age = current - seen
            if age < timedelta(hours=1):
                return FreshnessBucket.NEW_LAST_HOUR
            if age < timedelta(hours=6):
                return FreshnessBucket.NEW_LAST_6_HOURS
            if seen.date() == current.date():
                return FreshnessBucket.NEW_TODAY

        # Only a trusted posted_at may drive a "posted" bucket.
        posted = ensure_utc(posted_at) if posted_at_is_valid else None
        if posted is not None:
            if current - posted < timedelta(hours=24):
                return FreshnessBucket.POSTED_LAST_24_HOURS
            if posted.date() == current.date():
                return FreshnessBucket.POSTED_TODAY

        updated = ensure_utc(last_updated_at)
        if updated is not None and updated.date() == current.date():
            return FreshnessBucket.UPDATED_TODAY

        return FreshnessBucket.OLDER

    # ---- convenience ---------------------------------------------------------

    @staticmethod
    def window_start(bucket: FreshnessBucket, *, now: datetime | None = None) -> datetime | None:
        """Lower bound for a bucket, for building SQL predicates.

        Returns ``None`` for buckets that are not a simple time window.
        """
        current = ensure_utc(now) or utc_now()
        match bucket:
            case FreshnessBucket.NEW_LAST_HOUR:
                return current - timedelta(hours=1)
            case FreshnessBucket.NEW_LAST_6_HOURS:
                return current - timedelta(hours=6)
            case FreshnessBucket.POSTED_LAST_24_HOURS:
                return current - timedelta(hours=24)
            case (
                FreshnessBucket.NEW_TODAY
                | FreshnessBucket.POSTED_TODAY
                | FreshnessBucket.UPDATED_TODAY
            ):
                return datetime(current.year, current.month, current.day, tzinfo=UTC)
            case _:
                return None
