/**
 * Server-side API client.
 *
 * Every call runs on the Next.js server, never in the browser. That is what keeps the API
 * host (and anything it is trusted with) off the client, and it means the browser holds no
 * credentials of any kind — the spec's "never expose database credentials to the frontend"
 * requirement is structural here, not a convention.
 */

const API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8765/api/v1";

export type FreshnessBucket =
  | "NEW_LAST_HOUR"
  | "NEW_LAST_6_HOURS"
  | "NEW_TODAY"
  | "POSTED_LAST_24_HOURS"
  | "POSTED_TODAY"
  | "UPDATED_TODAY"
  | "OLDER"
  | "EXPIRED";

export interface JobSummary {
  id: number;
  title: string;
  company_id: number | null;
  company_name: string | null;
  city: string | null;
  state_code: string | null;
  country_code: string | null;
  remote_type: "REMOTE" | "HYBRID" | "ONSITE" | "UNKNOWN";
  employment_type: string;
  seniority: string | null;
  salary_min: string | null;
  salary_max: string | null;
  salary_currency: string | null;
  salary_interval: string;
  /** What the employer said. Null for ~19% of source rows. */
  posted_at: string | null;
  /** False when posted_at is missing, future-dated, or implausibly old. */
  posted_at_is_valid: boolean;
  /** When this platform first detected the job. Never a substitute for posted_at. */
  first_seen_at: string;
  last_seen_at: string;
  last_updated_at: string | null;
  status: string;
  apply_url: string | null;
  source: string;
  freshness: FreshnessBucket;
  /** Role category slug, derived from the title at ingestion. */
  category_slug: string | null;
  /** Derived seniority band, independent of category. */
  seniority_level: string;
  /** Employer industry, from the source's company registry (97% coverage). */
  industry: string | null;
}

export interface IndustryFacet {
  industry: string;
  job_count: number;
}

export interface CategoryFacet {
  slug: string;
  name: string;
  icon: string | null;
  job_count: number;
}

export interface SeniorityFacet {
  level: string;
  job_count: number;
}

export interface Company {
  id: number;
  name: string;
  website: string | null;
  industry: string | null;
  /** Open roles right now. Zero is a real answer, not a missing one. */
  active_job_count: number;
  /** Everything they have ever posted here, including expired roles. */
  total_job_count: number;
}

export interface CompanyPage {
  items: Company[];
  /** The whole directory, so paging cannot silently end early. */
  total: number;
}

export interface JobDetail extends JobSummary {
  description_text: string | null;
  description_html: string | null;
  department: string | null;
  job_url: string | null;
  close_at: string | null;
  closed_at: string | null;
  source_fetched_at: string | null;
  company_website: string | null;
  company_career_url: string | null;
  company_industry: string | null;
  source_count: number;
}

export interface PageMeta {
  next_cursor: string | null;
  has_more: boolean;
  page_size: number;
  total: number | null;
}

export interface JobPage {
  items: JobSummary[];
  meta: PageMeta;
}

export interface Stats {
  total_jobs: number;
  active_jobs: number;
  expired_jobs: number;
  remote_jobs: number;
  posted_last_hour: number;
  posted_last_6h: number;
  posted_last_24h: number;
  posted_today: number;
  detected_today: number;
  updated_today: number;
  unknown_posted_at: number;
  companies: number;
  /**
   * Start of the server-local day the *_today counters were measured against. Linking a
   * "today" tile through this instant is what keeps the tile and its drill-down equal —
   * the browser's own midnight is in a different timezone and would not agree.
   */
  day_start: string;
  /** Most recent SUCCEEDED ingest. Null before the first one finishes. */
  last_ingest_at: string | null;
  /** Non-zero while an ingest is in flight. */
  ingest_running: number;
  generated_at: string;
}

export interface StateCount {
  state_code: string;
  job_count: number;
}

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

const CACHE_SECONDS = 30;

/**
 * Paths whose URL is the same on every request, so the data cache holds one entry each.
 *
 * Membership is not about how hot an endpoint is -- it is about whether its URL space is
 * finite. /jobs/{id} is missing on purpose despite having no query string: one file per
 * job id is exactly the growth this list exists to prevent.
 */
const BOUNDED_PATHS: ReadonlySet<string> = new Set([
  "/stats",
  "/categories",
  "/seniority-levels",
  "/industries",
  "/locations/states",
  "/locations/states/counts",
]);

/**
 * Params whose value set is small enough not to multiply the cache.
 *
 * `country` has two values, so a bounded path with a country is still two entries, not the
 * open-ended set that filled a host's inodes. Nothing else belongs here: `state` alone
 * would be fifty times every path it appears on.
 */
const BOUNDED_PARAMS: ReadonlySet<string> = new Set(["country"]);

async function get<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  const url = new URL(API_BASE + path);
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) url.searchParams.append(key, String(item));
    } else {
      url.searchParams.set(key, String(value));
    }
  }

  // Job data is volatile, so a short revalidation window keeps the feed fresh without
  // hammering the API on every render -- but only where the URL space is bounded.
  //
  // Next writes one file per distinct fetch URL under .next/cache/fetch-cache and never
  // evicts it. Applying `revalidate` to every call meant every filter combination, every
  // cursor and every job id became a permanent file in the container's writable layer:
  // 2,251,122 files and 9.0 GB after thirteen days on production, which exhausted the
  // host's inodes and blocked deploys until the layer was dropped. Nothing in the app
  // noticed, because a cache that only grows still answers correctly.
  //
  // So caching is opt-in: a bounded path, carrying only bounded params. Everything else is
  // no-store, which costs nothing it was not already paying -- those pages are
  // `force-dynamic`, so the data cache was only collapsing repeat calls within a render.
  const cacheable =
    BOUNDED_PATHS.has(path) &&
    [...url.searchParams.keys()].every((key) => BOUNDED_PARAMS.has(key));

  const response = await fetch(url, {
    ...(cacheable ? { next: { revalidate: CACHE_SECONDS } } : { cache: "no-store" as const }),
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(`${path} failed: ${response.status} ${body.slice(0, 200)}`, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  jobs: (params: Record<string, unknown> = {}) => get<JobPage>("/jobs", params),
  latest: (params: Record<string, unknown> = {}) => get<JobPage>("/jobs/latest", params),
  recent: (params: Record<string, unknown> = {}) => get<JobPage>("/jobs/recent", params),
  job: (id: number | string) => get<JobDetail>(`/jobs/${id}`),
  search: (params: Record<string, unknown>) => get<JobPage>("/search", params),
  stats: (params: Record<string, unknown> = {}) => get<Stats>("/stats", params),
  stateCounts: (params: Record<string, unknown> = {}) =>
    get<StateCount[]>("/locations/states/counts", params),
  states: () => get<{ code: string; name: string }[]>("/locations/states"),
  categories: (params: Record<string, unknown> = {}) =>
    get<CategoryFacet[]>("/categories", params),
  seniorityLevels: (params: Record<string, unknown> = {}) =>
    get<SeniorityFacet[]>("/seniority-levels", params),
  industries: (params: Record<string, unknown> = {}) =>
    get<IndustryFacet[]>("/industries", params),
  companies: (params: Record<string, unknown> = {}) => get<CompanyPage>("/companies", params),
  ingestion: () => get<{ runs: unknown[]; rejections: unknown[] }>("/admin/ingestion"),
};

export { ApiError };
