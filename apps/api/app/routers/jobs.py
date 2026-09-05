"""Job endpoints.

Response shapes are deliberately explicit about time. Every job carries **both**
``posted_at`` (what the employer said) and ``first_seen_at`` (when this platform first
detected it), plus ``posted_at_is_valid``. The frontend can then label them separately and
never present a future-dated or missing timestamp as "posted 18 minutes ago".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobplatform_schemas import (
    Cursor,
    FreshnessBucket,
    PageMeta,
    decode_cursor,
    encode_cursor,
)
from jobplatform_shared import get_settings
from jobplatform_shared.db import get_db
from jobplatform_shared.time import hours_ago, utc_now

from ..repositories.jobs import SORT_OPTIONS, JobFilters, JobRepository

router = APIRouter()


# --------------------------------------------------------------------------- models


class JobSummary(BaseModel):
    """A job card. Everything the feed needs, nothing it does not."""

    id: int
    title: str
    company_id: int | None = None
    company_name: str | None = None
    city: str | None = None
    state_code: str | None = None
    country_code: str | None = None
    remote_type: str
    employment_type: str
    seniority: str | None = None
    #: Role category slug, e.g. "healthcare". Derived from the title at ingestion.
    category_slug: str | None = None
    #: Derived seniority band, independent of category.
    seniority_level: str = "UNKNOWN"
    #: Employer industry, from the source's company registry (97% coverage).
    industry: str | None = None

    salary_min: Decimal | None = None
    salary_max: Decimal | None = None
    salary_currency: str | None = None
    salary_interval: str

    #: What the employer said. May be null, and may be untrustworthy — see the flag.
    posted_at: datetime | None = None
    #: False when posted_at is missing, future-dated, or implausibly old. The UI must not
    #: render a relative time when this is false.
    posted_at_is_valid: bool
    #: When THIS platform first detected the job. Never a substitute for posted_at.
    first_seen_at: datetime
    last_seen_at: datetime
    last_updated_at: datetime | None = None

    status: str
    apply_url: str | None = None
    source: str
    freshness: FreshnessBucket


class JobDetail(JobSummary):
    description_text: str | None = None
    description_html: str | None = None
    department: str | None = None
    job_url: str | None = None
    close_at: datetime | None = None
    closed_at: datetime | None = None
    source_fetched_at: datetime | None = None
    company_website: str | None = None
    company_career_url: str | None = None
    company_industry: str | None = None
    #: How many sources describe this job. >1 means it was deduplicated across sources.
    source_count: int = 1


class JobPage(BaseModel):
    items: list[JobSummary]
    meta: PageMeta


class StatsResponse(BaseModel):
    total_jobs: int
    active_jobs: int
    expired_jobs: int
    remote_jobs: int
    posted_last_hour: int
    posted_last_6h: int
    posted_last_24h: int
    posted_today: int
    detected_today: int
    updated_today: int
    unknown_posted_at: int
    companies: int
    #: Start of the server-local day the *_today counters were measured against.
    day_start: datetime
    #: Finish time of the most recent SUCCEEDED ingest. None before the first one
    #: completes. This is when the data last changed -- generated_at below is only
    #: when this response was assembled, which says nothing about freshness.
    last_ingest_at: datetime | None = None
    #: Non-zero while an ingest is in flight, so the UI can say so rather than
    #: showing a stale timestamp with no explanation.
    ingest_running: int = 0
    generated_at: datetime


# ------------------------------------------------------------------------ helpers


def _freshness(row: dict[str, Any], now: datetime) -> FreshnessBucket:
    """Derive the bucket at query time.

    Never stored: "posted today" goes stale every minute, so a persisted value would be
    wrong for most of its life.
    """
    if row["status"] in {"EXPIRED", "REMOVED"}:
        return FreshnessBucket.EXPIRED

    first_seen = row.get("first_seen_at")
    if first_seen is not None:
        age = (now - first_seen).total_seconds()
        if age < 3600:
            return FreshnessBucket.NEW_LAST_HOUR
        if age < 6 * 3600:
            return FreshnessBucket.NEW_LAST_6_HOURS
        if first_seen.date() == now.date():
            return FreshnessBucket.NEW_TODAY

    if row.get("posted_at_is_valid") and row.get("posted_at") is not None:
        posted = row["posted_at"]
        if (now - posted).total_seconds() < 24 * 3600:
            return FreshnessBucket.POSTED_LAST_24_HOURS
        if posted.date() == now.date():
            return FreshnessBucket.POSTED_TODAY

    updated = row.get("last_updated_at")
    if updated is not None and updated.date() == now.date():
        return FreshnessBucket.UPDATED_TODAY

    return FreshnessBucket.OLDER


def _page(rows: list[dict[str, Any]], has_more: bool, sort: str, limit: int) -> JobPage:
    now = utc_now()
    items = [JobSummary(**row, freshness=_freshness(row, now)) for row in rows]

    next_cursor = None
    if has_more and rows and SORT_OPTIONS[sort]["keyset"]:
        last = rows[-1]
        key = {
            "posted_at_desc": "posted_at",
            "first_seen_desc": "first_seen_at",
            "salary_desc": "salary_max",
        }[sort]
        value = last[key]
        next_cursor = encode_cursor(
            Cursor(
                value=value.isoformat() if isinstance(value, datetime) else value,
                id=last["id"],
                sort=sort,
            )
        )

    return JobPage(
        items=items,
        meta=PageMeta(next_cursor=next_cursor, has_more=has_more, page_size=limit),
    )


def _parse_cursor(raw: str | None, sort: str) -> Cursor | None:
    if not raw:
        return None
    try:
        cursor = decode_cursor(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="malformed cursor"
        ) from exc
    if cursor and cursor.sort != sort:
        # Silently restarting would look to the client like duplicated results.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="cursor was issued for a different sort order",
        )
    return cursor


# ------------------------------------------------------------------------- routes


@router.get("/jobs", response_model=JobPage, summary="List jobs")
async def list_jobs(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=200, description="Full-text query")] = None,
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
    state: Annotated[str | None, Query(min_length=2, max_length=3)] = None,
    city: Annotated[str | None, Query(max_length=100)] = None,
    company_id: int | None = None,
    category: Annotated[
        list[str] | None, Query(description="Role category slug, repeatable")
    ] = None,
    seniority: Annotated[
        list[str] | None, Query(description="ENTRY | MID | SENIOR | LEAD | MANAGER | ...")
    ] = None,
    industry: Annotated[list[str] | None, Query(description="Employer industry")] = None,
    remote: Annotated[list[str] | None, Query(description="REMOTE | HYBRID | ONSITE")] = None,
    employment_type: Annotated[list[str] | None, Query()] = None,
    salary_min: Annotated[float | None, Query(ge=0, le=100_000_000)] = None,
    posted_since: datetime | None = None,
    posted_within_hours: Annotated[int | None, Query(ge=1, le=8760)] = None,
    seen_since: datetime | None = None,
    seen_within_hours: Annotated[int | None, Query(ge=1, le=8760)] = None,
    job_status: Annotated[str, Query(alias="status")] = "ACTIVE",
    sort: Literal[
        "posted_at_desc", "first_seen_desc", "salary_desc", "relevance"
    ] = "posted_at_desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> JobPage:
    """Cursor-paginated job listing.

    Sorting by ``relevance`` requires ``q`` and does not support cursors — relevance is
    computed per query, so it cannot form a stable keyset.
    """
    settings = get_settings()
    limit = min(limit, settings.api_max_page_size)

    if sort == "relevance" and not q:
        sort = "posted_at_desc"

    filters = JobFilters(
        q=q,
        country=country,
        state=state,
        city=city,
        company_id=company_id,
        category=[c.strip().lower() for c in (category or []) if c.strip()],
        seniority=[s.strip().upper() for s in (seniority or []) if s.strip()],
        industry=[i.strip() for i in (industry or []) if i.strip()],
        remote=[r.upper() for r in (remote or [])],
        employment_type=[e.upper() for e in (employment_type or [])],
        status=job_status.upper() if job_status.lower() != "any" else None,
        salary_min=salary_min,
        posted_since=(
            posted_since
            if posted_since
            else hours_ago(posted_within_hours)
            if posted_within_hours
            else None
        ),
        seen_since=(
            seen_since
            if seen_since
            else hours_ago(seen_within_hours)
            if seen_within_hours
            else None
        ),
    )

    repo = JobRepository(session)
    try:
        rows, has_more = await repo.search(
            filters, sort=sort, limit=limit, cursor=_parse_cursor(cursor, sort)
        )
    except ValueError as exc:
        # A cursor whose payload does not match its declared sort is a client error.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    # Job data is volatile; a long cache would show a stale feed on the freshest surface.
    response.headers["Cache-Control"] = "public, max-age=30"
    return _page(rows, has_more, sort, limit)


@router.get("/jobs/latest", response_model=JobPage, summary="Newest detected jobs")
async def latest_jobs(
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> JobPage:
    """Ordered by when THIS platform first saw the job.

    Distinct from ``/jobs/today``: this is our detection clock, which is always available,
    whereas ``posted_at`` is missing or untrustworthy for a fifth of records.
    """
    repo = JobRepository(session)
    rows, has_more = await repo.search(
        JobFilters(status="ACTIVE"),
        sort="first_seen_desc",
        limit=limit,
        cursor=_parse_cursor(cursor, "first_seen_desc"),
    )
    return _page(rows, has_more, "first_seen_desc", limit)


@router.get("/jobs/today", response_model=JobPage, summary="Posted today")
async def jobs_today(
    session: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> JobPage:
    """Only jobs with a **trustworthy** posted_at inside the last 24 hours."""
    now = utc_now()
    repo = JobRepository(session)
    rows, has_more = await repo.search(
        JobFilters(
            status="ACTIVE",
            posted_since=now.replace(hour=0, minute=0, second=0, microsecond=0),
            require_valid_posted_at=True,
        ),
        sort="posted_at_desc",
        limit=limit,
        cursor=_parse_cursor(cursor, "posted_at_desc"),
    )
    return _page(rows, has_more, "posted_at_desc", limit)


@router.get("/jobs/recent", response_model=JobPage, summary="Posted in the last N hours")
async def jobs_recent(
    session: Annotated[AsyncSession, Depends(get_db)],
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> JobPage:
    repo = JobRepository(session)
    rows, has_more = await repo.search(
        JobFilters(status="ACTIVE", posted_since=hours_ago(hours), require_valid_posted_at=True),
        sort="posted_at_desc",
        limit=limit,
        cursor=_parse_cursor(cursor, "posted_at_desc"),
    )
    return _page(rows, has_more, "posted_at_desc", limit)


@router.get("/jobs/{job_id}", response_model=JobDetail, summary="Job detail")
async def get_job(
    job_id: int,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JobDetail:
    repo = JobRepository(session)
    row = await repo.get(job_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobDetail(**row, freshness=_freshness(row, utc_now()))


@router.get("/search", response_model=JobPage, summary="Full-text search")
async def search_jobs(
    session: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
    state: Annotated[str | None, Query(min_length=2, max_length=3)] = None,
    category: Annotated[list[str] | None, Query()] = None,
    seniority: Annotated[list[str] | None, Query()] = None,
    remote: Annotated[list[str] | None, Query()] = None,
    salary_min: Annotated[float | None, Query(ge=0)] = None,
    sort: Literal["relevance", "posted_at_desc", "salary_desc"] = "relevance",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> JobPage:
    repo = JobRepository(session)
    filters = JobFilters(
        q=q,
        country=country,
        state=state,
        category=[c.strip().lower() for c in (category or []) if c.strip()],
        seniority=[s.strip().upper() for s in (seniority or []) if s.strip()],
        remote=[r.upper() for r in (remote or [])],
        salary_min=salary_min,
        status="ACTIVE",
    )
    rows, has_more = await repo.search(
        filters, sort=sort, limit=limit, cursor=_parse_cursor(cursor, sort)
    )
    return _page(rows, has_more, sort, limit)


@router.get("/stats", response_model=StatsResponse, summary="Platform statistics")
async def stats(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
) -> StatsResponse:
    """Every figure is scoped to one country, because the board is."""
    repo = JobRepository(session)
    data = await repo.stats(country=country)
    response.headers["Cache-Control"] = "public, max-age=60"
    return StatsResponse(**data, generated_at=utc_now())


@router.get("/categories", summary="Role categories with job counts")
async def categories(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    state: Annotated[str | None, Query(min_length=2, max_length=3)] = None,
    remote: Annotated[list[str] | None, Query()] = None,
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
) -> list[dict[str, Any]]:
    """Counts respect the other active filters, so a chip reads "Healthcare (312)" for
    the current view rather than a misleading nationwide total."""
    filters = JobFilters(
        country=country,
        state=state,
        remote=[r.upper() for r in (remote or [])],
        status="ACTIVE",
    )
    response.headers["Cache-Control"] = "public, max-age=120"
    return await JobRepository(session).category_facets(filters)


@router.get("/seniority-levels", summary="Seniority levels with job counts")
async def seniority_levels(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    state: Annotated[str | None, Query(min_length=2, max_length=3)] = None,
    category: Annotated[list[str] | None, Query()] = None,
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
) -> list[dict[str, Any]]:
    filters = JobFilters(
        country=country,
        state=state,
        category=[c.strip().lower() for c in (category or []) if c.strip()],
        status="ACTIVE",
    )
    response.headers["Cache-Control"] = "public, max-age=120"
    return await JobRepository(session).seniority_facets(filters)


@router.get("/industries", summary="Employer industries with job counts")
async def industries(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_db)],
    state: Annotated[str | None, Query(min_length=2, max_length=3)] = None,
    category: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
) -> list[dict[str, Any]]:
    """Industry is a second axis to role category, not a substitute for it."""
    filters = JobFilters(
        country=country,
        state=state,
        category=[c.strip().lower() for c in (category or []) if c.strip()],
        status="ACTIVE",
    )
    response.headers["Cache-Control"] = "public, max-age=120"
    return await JobRepository(session).industry_facets(filters, limit=limit)


@router.get("/locations/states/counts", summary="Active job count per state")
async def state_counts(
    session: Annotated[AsyncSession, Depends(get_db)],
    country: Annotated[str, Query(min_length=2, max_length=2)] = "US",
) -> list[dict[str, Any]]:
    return await JobRepository(session).by_state(country=country)


@router.get("/companies", summary="Employer directory")
async def companies(
    session: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str | None, Query(max_length=100, description="Filter by company name")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> dict[str, Any]:
    """Every employer that has posted at least once, most actively hiring first.

    ``total`` is the same population as the ``companies`` figure in /stats, so a client
    can page through all of it without the count and the list disagreeing.
    """
    items, total = await JobRepository(session).companies(limit=limit, offset=offset, q=q)
    return {"items": items, "total": total}


@router.get("/admin/ingestion", summary="Ingestion health")
async def ingestion_health(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """Latest run per source plus the rejection breakdown.

    Unauthenticated for now because it exposes only aggregates. It moves behind the admin
    role in Milestone 12, when authentication lands.
    """
    repo = JobRepository(session)
    return {
        "runs": await repo.ingestion_health(),
        "rejections": await repo.rejection_breakdown(),
    }


class IngestRequestState(BaseModel):
    """What the "load new jobs" button should show right now."""

    id: int | None = None
    status: str  # QUEUED | RUNNING | SUCCEEDED | FAILED | SKIPPED | IDLE
    message: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    #: True while anything is in flight, so the button disables itself rather than
    #: letting an impatient second click queue a duplicate run.
    busy: bool = False
    #: Seconds until another fetch may be requested. Zero when one may be made now.
    retry_after: int = 0


#: Minimum gap between completed fetches. This endpoint is unauthenticated -- there is no
#: login in this deployment -- so the cooldown is what stops anyone with the URL from
#: making the server ingest continuously. It bounds abuse to roughly what the daily
#: schedule already does, and costs a real operator nothing: a fetch takes longer than
#: this anyway.
_FETCH_COOLDOWN_SECONDS: Final = 600


@router.post(
    "/admin/ingest",
    response_model=IngestRequestState,
    status_code=202,
    summary="Fetch jobs from the source now",
)
async def request_ingest(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> IngestRequestState:
    """Queue an on-demand fetch.

    202, not 200: the work is accepted, not done. The API cannot run the ingest itself --
    it has no Docker socket, and giving it one would let any request-handling bug start
    privileged containers on the host. A timer claims the row, usually within a minute.
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, status::text AS status, created_at, finished_at,"
                    "       EXTRACT(EPOCH FROM (now() - finished_at))::bigint AS since_finished"
                    "  FROM ingest_requests ORDER BY id DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .first()
    )

    # One at a time. A second request while one is in flight is almost always an impatient
    # second click, and queueing it would do the same work twice for no benefit.
    if row and row["status"] in ("QUEUED", "RUNNING"):
        return IngestRequestState(
            id=row["id"],
            status=row["status"],
            busy=True,
            message="A fetch is already in progress.",
            created_at=row["created_at"],
        )

    if row and row["since_finished"] is not None:
        elapsed = int(row["since_finished"])
        if elapsed < _FETCH_COOLDOWN_SECONDS:
            remaining = _FETCH_COOLDOWN_SECONDS - elapsed
            return IngestRequestState(
                id=row["id"],
                status=row["status"],
                busy=False,
                retry_after=remaining,
                message=f"Recently fetched. You can fetch again in {remaining // 60 + 1} min.",
                created_at=row["created_at"],
                finished_at=row["finished_at"],
            )

    # Caddy sets X-Forwarded-For; its own address would be useless here.
    caller = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else None
    )

    created = (
        (
            await session.execute(
                text(
                    "INSERT INTO ingest_requests (requested_by) VALUES (:by)"
                    " RETURNING id, status::text AS status, created_at"
                ),
                {"by": caller},
            )
        )
        .mappings()
        .one()
    )
    await session.commit()

    return IngestRequestState(
        id=created["id"],
        status=created["status"],
        busy=True,
        message="Fetch queued. It will start within a minute.",
        created_at=created["created_at"],
    )


@router.get("/admin/ingest", response_model=IngestRequestState, summary="Fetch status")
async def ingest_request_status(
    session: Annotated[AsyncSession, Depends(get_db)],
) -> IngestRequestState:
    """The most recent request, whatever became of it."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT id, status::text AS status, message, created_at, finished_at,"
                    "       EXTRACT(EPOCH FROM (now() - finished_at))::bigint AS since_finished"
                    "  FROM ingest_requests ORDER BY id DESC LIMIT 1"
                )
            )
        )
        .mappings()
        .first()
    )

    if not row:
        return IngestRequestState(status="IDLE")

    busy = row["status"] in ("QUEUED", "RUNNING")
    remaining = 0
    if not busy and row["since_finished"] is not None:
        remaining = max(0, _FETCH_COOLDOWN_SECONDS - int(row["since_finished"]))

    return IngestRequestState(
        id=row["id"],
        status=row["status"],
        message=row["message"],
        created_at=row["created_at"],
        finished_at=row["finished_at"],
        busy=busy,
        retry_after=remaining,
    )
