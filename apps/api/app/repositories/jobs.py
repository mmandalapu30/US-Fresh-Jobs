"""Job read repository.

All SQL for the read path lives here. Routers and services never build SQL, which is what
makes the OpenSearch swap in a later milestone a repository substitution rather than a
rewrite.

Two things this file is careful about:

* **Keyset pagination, not OFFSET.** ``OFFSET 500000`` makes PostgreSQL walk half a million
  rows before returning anything. Keyset on ``(sort_key, id)`` stays O(log n) at any depth,
  which is what the "50M+ historical jobs" target requires.
* **Bound parameters everywhere.** Filter values never reach the SQL string. Sort keys are
  resolved through a fixed allowlist, so an ``ORDER BY`` clause cannot be injected either.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from jobplatform_schemas import Cursor

__all__ = ["SORT_OPTIONS", "JobFilters", "JobRepository"]

SortKey = Literal["posted_at_desc", "first_seen_desc", "salary_desc", "relevance"]

#: Allowlisted sorts. The SQL fragments are constants — never built from input — so the
#: ORDER BY clause cannot be influenced by a request.
SORT_OPTIONS: dict[str, dict[str, str]] = {
    "posted_at_desc": {
        "order": "j.posted_at DESC NULLS LAST, j.id DESC",
        "keyset": "(j.posted_at, j.id) < (CAST(:cursor_value AS timestamptz), :cursor_id)",
        "column": "j.posted_at",
        "value_type": "timestamp",
    },
    "first_seen_desc": {
        "order": "j.first_seen_at DESC, j.id DESC",
        "keyset": "(j.first_seen_at, j.id) < (CAST(:cursor_value AS timestamptz), :cursor_id)",
        "column": "j.first_seen_at",
        "value_type": "timestamp",
    },
    "salary_desc": {
        "order": "j.salary_max DESC NULLS LAST, j.id DESC",
        "keyset": "(j.salary_max, j.id) < (CAST(:cursor_value AS numeric), :cursor_id)",
        "column": "j.salary_max",
        "value_type": "numeric",
    },
    "relevance": {
        "order": "rank DESC, j.id DESC",
        "keyset": "",  # relevance is not keyset-safe; falls back to a bounded offset
        "column": "rank",
        "value_type": "none",
    },
}


def _coerce_cursor_value(value: Any, value_type: str) -> Any:
    """Convert a cursor's JSON value back to the Python type the column expects.

    A cursor round-trips through JSON, so a timestamp arrives as a string. asyncpg binds
    by inferred parameter type and refuses a ``str`` for a ``timestamptz`` argument -- the
    SQL-level CAST does not help, because the driver rejects it before PostgreSQL sees it.
    Without this, every "next page" on a date-ordered feed returned 500.
    """
    if value is None:
        return None
    if value_type == "timestamp":
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"cursor carries an unparseable timestamp: {value!r}") from exc
    if value_type == "numeric":
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError) as exc:
            raise ValueError(f"cursor carries an unparseable number: {value!r}") from exc
    return value


@dataclass(slots=True)
class JobFilters:
    """Every filter the API exposes. All optional; unset means "no constraint"."""

    q: str | None = None
    country: str = "US"
    state: str | None = None
    city: str | None = None
    company_id: int | None = None
    #: Role categories (job function). Multi-select: several categories OR together.
    category: list[str] = field(default_factory=list)
    #: Seniority levels. Also multi-select.
    seniority: list[str] = field(default_factory=list)
    #: Employer industry — a second axis to category. A nurse at a hospital and a nurse
    #: at a school share a category but not an industry.
    industry: list[str] = field(default_factory=list)
    remote: list[str] = field(default_factory=list)
    employment_type: list[str] = field(default_factory=list)
    status: str | None = "ACTIVE"
    salary_min: float | None = None
    posted_since: datetime | None = None
    seen_since: datetime | None = None
    #: Restrict to jobs whose posted_at is trustworthy. The freshness surfaces set this so
    #: a future-dated row never appears as "posted recently".
    require_valid_posted_at: bool = False


class JobRepository:
    """Read access to jobs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- listing -------------------------------------------------------------

    async def search(
        self,
        filters: JobFilters,
        *,
        sort: str = "posted_at_desc",
        limit: int = 25,
        cursor: Cursor | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return one page plus whether more exist.

        Fetches ``limit + 1`` rows: the extra row answers "is there a next page" without a
        second query or a COUNT.
        """
        if sort not in SORT_OPTIONS:
            sort = "posted_at_desc"
        spec = SORT_OPTIONS[sort]

        where, params = self._build_where(filters)

        # Keyset predicate. Skipped for relevance sorting, where the sort value is
        # computed per query and cannot form a stable key.
        if cursor is not None and spec["keyset"]:
            where.append(spec["keyset"])
            params["cursor_value"] = _coerce_cursor_value(cursor.value, spec["value_type"])
            params["cursor_id"] = cursor.id

        params["limit"] = limit + 1

        rank_select = ""
        if filters.q:
            rank_select = ", ts_rank(j.search_vector, websearch_to_tsquery('english', :q)) AS rank"

        sql = f"""
            SELECT
                j.id, j.title, j.city, j.state_code, j.country_code,
                j.remote_type, j.employment_type, j.category_slug, j.seniority_level,
                j.industry,
                j.salary_min, j.salary_max, j.salary_currency, j.salary_interval,
                j.posted_at, j.posted_at_is_valid, j.first_seen_at, j.last_seen_at,
                j.last_updated_at, j.status, j.apply_url, j.source, j.seniority,
                c.id AS company_id, c.name AS company_name
                {rank_select}
            FROM jobs j
            LEFT JOIN companies c ON c.id = j.company_id
            WHERE {" AND ".join(where)}
            ORDER BY {spec["order"]}
            LIMIT :limit
        """  # noqa: S608 - every fragment is a module constant; values are bound parameters

        rows = (await self._session.execute(text(sql), params)).mappings().all()
        has_more = len(rows) > limit
        return [dict(row) for row in rows[:limit]], has_more

    async def get(self, job_id: int) -> dict[str, Any] | None:
        """Full detail for one job, including both timestamps the UI must show."""
        sql = """
            SELECT
                j.*,
                c.name AS company_name, c.website AS company_website,
                c.career_url AS company_career_url, c.industry AS company_industry,
                c.size_range AS company_size,
                (SELECT count(*) FROM job_sources s WHERE s.job_id = j.id) AS source_count
            FROM jobs j
            LEFT JOIN companies c ON c.id = j.company_id
            WHERE j.id = :job_id
        """
        row = (await self._session.execute(text(sql), {"job_id": job_id})).mappings().first()
        return dict(row) if row else None

    async def count(self, filters: JobFilters) -> int:
        """Exact count for a filter set.

        Deliberately separate from ``search``: on tens of millions of rows a COUNT can cost
        more than the page itself, so endpoints opt in rather than paying for it always.
        """
        where, params = self._build_where(filters)
        sql = f"SELECT count(*) FROM jobs j WHERE {' AND '.join(where)}"  # noqa: S608
        return int((await self._session.execute(text(sql), params)).scalar_one())

    # ---- aggregates ----------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Headline numbers for the homepage and the admin dashboard."""
        sql = """
            SELECT
                count(*)                                                   AS total_jobs,
                count(*) FILTER (WHERE status = 'ACTIVE')                  AS active_jobs,
                count(*) FILTER (WHERE status = 'EXPIRED')                 AS expired_jobs,
                count(*) FILTER (WHERE country_code = 'US')                AS us_jobs,
                count(*) FILTER (WHERE remote_type = 'REMOTE'
                                   AND status = 'ACTIVE')                  AS remote_jobs,
                count(*) FILTER (WHERE posted_at_is_valid
                                   AND posted_at >= now() - interval '1 hour')  AS posted_last_hour,
                count(*) FILTER (WHERE posted_at_is_valid
                                   AND posted_at >= now() - interval '6 hours') AS posted_last_6h,
                count(*) FILTER (WHERE posted_at_is_valid
                                   AND posted_at >= date_trunc('day', now()))   AS posted_today,
                count(*) FILTER (WHERE posted_at_is_valid
                                   AND posted_at >= now() - interval '24 hours') AS posted_last_24h,
                count(*) FILTER (WHERE first_seen_at >= date_trunc('day', now())) AS detected_today,
                count(*) FILTER (WHERE last_updated_at >= date_trunc('day', now())) AS updated_today,
                count(*) FILTER (WHERE NOT posted_at_is_valid)             AS unknown_posted_at,
                count(DISTINCT company_id)                                 AS companies,
                -- The exact instant the "today" filters above were measured from. Exposed
                -- so a client can drill into those counts on the same boundary instead of
                -- guessing at it from its own clock and quietly disagreeing.
                date_trunc('day', now())                                   AS day_start
            FROM jobs
        """
        row = (await self._session.execute(text(sql))).mappings().one()
        return dict(row)

    async def category_facets(self, filters: JobFilters | None = None) -> list[dict[str, Any]]:
        """Job counts per category, honouring the other active filters.

        Counting under the current filters is what makes the chips useful: "Healthcare
        (312)" while a state is selected means 312 *in that state*, not nationwide.
        """
        base = filters or JobFilters()
        # Clear the category filter itself, otherwise every other chip would read zero.
        scoped = replace(base, category=[])
        where, params = self._build_where(scoped)

        sql = f"""
            SELECT c.slug, c.name, c.icon, count(j.id) AS job_count
              FROM job_categories c
              LEFT JOIN jobs j
                     ON j.category_slug = c.slug AND {" AND ".join(where)}
             GROUP BY c.slug, c.name, c.icon, c.sort_order
             ORDER BY job_count DESC, c.sort_order
        """  # noqa: S608 - fragments are constants; values are bound
        rows = (await self._session.execute(text(sql), params)).mappings().all()
        return [dict(row) for row in rows]

    async def seniority_facets(self, filters: JobFilters | None = None) -> list[dict[str, Any]]:
        base = filters or JobFilters()
        scoped = replace(base, seniority=[])
        where, params = self._build_where(scoped)

        sql = f"""
            SELECT seniority_level::text AS level, count(*) AS job_count
              FROM jobs j
             WHERE {" AND ".join(where)}
             GROUP BY seniority_level
             ORDER BY job_count DESC
        """  # noqa: S608
        rows = (await self._session.execute(text(sql), params)).mappings().all()
        return [dict(row) for row in rows]

    async def industry_facets(
        self, filters: JobFilters | None = None, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Top industries under the current filters.

        Limited: the source has hundreds of industry strings with a long tail of one-job
        values, which would make an unbounded list unusable as a filter.
        """
        base = filters or JobFilters()
        scoped = replace(base, industry=[])
        where, params = self._build_where(scoped)
        params["limit"] = limit

        sql = f"""
            SELECT industry, count(*) AS job_count
              FROM jobs j
             WHERE {" AND ".join(where)} AND j.industry IS NOT NULL
             GROUP BY industry
             ORDER BY job_count DESC
             LIMIT :limit
        """  # noqa: S608 - fragments are constants; values are bound
        rows = (await self._session.execute(text(sql), params)).mappings().all()
        return [dict(row) for row in rows]

    async def by_state(self, *, limit: int = 60) -> list[dict[str, Any]]:
        sql = """
            SELECT state_code, count(*) AS job_count
              FROM jobs
             WHERE country_code = 'US' AND status = 'ACTIVE' AND state_code IS NOT NULL
             GROUP BY state_code
             ORDER BY job_count DESC
             LIMIT :limit
        """
        rows = (await self._session.execute(text(sql), {"limit": limit})).mappings().all()
        return [dict(row) for row in rows]

    async def companies(
        self, *, limit: int = 50, offset: int = 0, q: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """The employer directory, ordered by how much each is hiring right now.

        Every company that has ever posted is listed, including those whose postings have
        all expired — they carry a zero active count instead of disappearing. Jobs are
        never deleted here, so the employers behind them do not vanish either, which is
        also what keeps ``total`` equal to the ``companies`` figure in /stats.
        """
        where = "TRUE"
        params: dict[str, Any] = {}
        if q and q.strip():
            where = "c.name ILIKE :pattern"
            params["pattern"] = f"%{q.strip()}%"

        total_sql = f"""
            SELECT count(DISTINCT c.id)
              FROM companies c
              JOIN jobs j ON j.company_id = c.id
             WHERE {where}
        """  # noqa: S608 - `where` is one of two module constants; the value is bound
        total = int((await self._session.execute(text(total_sql), params)).scalar_one())

        # c.id breaks ties last, so a company can never repeat across offset pages.
        sql = f"""
            SELECT c.id,
                   c.name,
                   c.website,
                   c.industry,
                   count(*) FILTER (WHERE j.status = 'ACTIVE') AS active_job_count,
                   count(*)                                    AS total_job_count
              FROM companies c
              JOIN jobs j ON j.company_id = c.id
             WHERE {where}
             GROUP BY c.id, c.name, c.website, c.industry
             ORDER BY active_job_count DESC, total_job_count DESC, c.name ASC, c.id ASC
             LIMIT :limit OFFSET :offset
        """  # noqa: S608 - same: constant fragment, bound values
        rows = (
            (await self._session.execute(text(sql), {**params, "limit": limit, "offset": offset}))
            .mappings()
            .all()
        )
        return [dict(row) for row in rows], total

    async def ingestion_health(self) -> list[dict[str, Any]]:
        """Most recent sync run per source. Drives the admin panel and staleness alerts."""
        sql = """
            SELECT DISTINCT ON (source)
                   source, status, started_at, finished_at, duration_seconds,
                   files_processed, rows_processed, rows_accepted, rows_rejected,
                   rows_inserted, rows_updated, duplicates_found
              FROM sync_runs
             ORDER BY source, started_at DESC
        """
        rows = (await self._session.execute(text(sql))).mappings().all()
        return [dict(row) for row in rows]

    async def rejection_breakdown(self, *, limit: int = 20) -> list[dict[str, Any]]:
        sql = """
            SELECT reason, sum(occurrence_count)::bigint AS n
              FROM sync_errors
             GROUP BY reason
             ORDER BY n DESC
             LIMIT :limit
        """
        rows = (await self._session.execute(text(sql), {"limit": limit})).mappings().all()
        return [dict(row) for row in rows]

    # ---- where clause --------------------------------------------------------

    @staticmethod
    def _build_where(filters: JobFilters) -> tuple[list[str], dict[str, Any]]:
        """Compose the predicate. Every value is bound, never interpolated."""
        where: list[str] = ["TRUE"]
        params: dict[str, Any] = {}

        if filters.country:
            where.append("j.country_code = :country")
            params["country"] = filters.country.upper()

        if filters.status:
            where.append("j.status = CAST(:status AS job_status)")
            params["status"] = filters.status

        if filters.state:
            where.append("j.state_code = :state")
            params["state"] = filters.state.upper()

        if filters.city:
            # Case-insensitive prefix match; the trigram index on city keeps it cheap.
            where.append("j.city ILIKE :city")
            params["city"] = f"{filters.city}%"

        if filters.company_id:
            where.append("j.company_id = :company_id")
            params["company_id"] = filters.company_id

        if filters.category:
            where.append("j.category_slug = ANY(:category)")
            params["category"] = filters.category

        if filters.seniority:
            where.append("j.seniority_level = ANY(CAST(:seniority AS seniority_level[]))")
            params["seniority"] = filters.seniority

        if filters.industry:
            where.append("j.industry = ANY(:industry)")
            params["industry"] = filters.industry

        if filters.remote:
            where.append("j.remote_type = ANY(CAST(:remote AS remote_type[]))")
            params["remote"] = filters.remote

        if filters.employment_type:
            where.append("j.employment_type = ANY(CAST(:employment AS employment_type[]))")
            params["employment"] = filters.employment_type

        if filters.salary_min is not None:
            # Compare against the top of the range: a job paying "up to 120k" satisfies a
            # "at least 100k" filter, whereas comparing salary_min would exclude it.
            where.append("j.salary_max >= :salary_min")
            params["salary_min"] = filters.salary_min

        if filters.posted_since is not None:
            where.append("j.posted_at >= :posted_since AND j.posted_at_is_valid")
            params["posted_since"] = filters.posted_since

        if filters.seen_since is not None:
            where.append("j.first_seen_at >= :seen_since")
            params["seen_since"] = filters.seen_since

        if filters.require_valid_posted_at:
            where.append("j.posted_at_is_valid")

        if filters.q:
            # websearch_to_tsquery accepts human phrasing ("python -django" / quoted
            # phrases) and, unlike to_tsquery, never raises on malformed input — so a
            # hostile query string cannot produce a 500.
            where.append("j.search_vector @@ websearch_to_tsquery('english', :q)")
            params["q"] = filters.q

        return where, params
