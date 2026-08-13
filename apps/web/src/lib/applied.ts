/**
 * "Applied" is per-browser state.
 *
 * There is no authentication yet (Milestone 12), so there is no account to hang this on.
 * localStorage is the honest place for it: it belongs to this browser and nothing else,
 * and the UI says so rather than implying a synced account.
 *
 * Two decisions here are load-bearing:
 *
 * 1. The store is a map keyed by job id, so one job can never occupy two slots.
 *    Duplicates are impossible by construction rather than filtered out afterwards.
 * 2. It keeps a snapshot of the job, not just its id. Jobs expire, and the feed API
 *    returns only ACTIVE rows by default — an application you actually sent must not
 *    disappear from your own list because the employer closed the posting. This mirrors
 *    the platform rule that jobs are never deleted, only transitioned.
 */

import type { JobSummary } from "./api";

export const STORAGE_KEY = "jobsearch.applied.v1";

export interface AppliedEntry {
  job: JobSummary;
  /** ISO-8601. When *this browser* marked it applied — never a source timestamp. */
  applied_at: string;
}

/** Keyed by `String(job.id)`. */
export type AppliedMap = Record<string, AppliedEntry>;

export function readApplied(): AppliedMap {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};

    // Validate on the way in. This store outlives any one deploy, so a shape written by
    // an older build must degrade to "not applied" rather than crash the page.
    const entries: AppliedMap = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const entry = value as Partial<AppliedEntry> | null;
      const job = entry?.job as JobSummary | undefined;
      if (job && typeof job === "object" && typeof job.id === "number") {
        entries[key] = { job, applied_at: entry?.applied_at ?? new Date(0).toISOString() };
      }
    }
    return entries;
  } catch {
    // Corrupt JSON, or storage blocked entirely. Neither is worth taking the page down for.
    return {};
  }
}

export function writeApplied(entries: AppliedMap): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // Quota exceeded or private-mode storage. The in-memory state still serves this
    // session; silently losing the write is better than losing the interaction.
  }
}

/**
 * Reduce a job to exactly what a card renders before storing it.
 *
 * The detail page hands over a JobDetail, which carries the full description HTML — tens
 * of kilobytes each. Storing those verbatim would exhaust the storage quota after a few
 * dozen applications and start silently dropping writes.
 */
export function toSummary(job: JobSummary): JobSummary {
  return {
    id: job.id,
    title: job.title,
    company_id: job.company_id,
    company_name: job.company_name,
    city: job.city,
    state_code: job.state_code,
    country_code: job.country_code,
    remote_type: job.remote_type,
    employment_type: job.employment_type,
    seniority: job.seniority,
    salary_min: job.salary_min,
    salary_max: job.salary_max,
    salary_currency: job.salary_currency,
    salary_interval: job.salary_interval,
    posted_at: job.posted_at,
    posted_at_is_valid: job.posted_at_is_valid,
    first_seen_at: job.first_seen_at,
    last_seen_at: job.last_seen_at,
    last_updated_at: job.last_updated_at,
    status: job.status,
    apply_url: job.apply_url,
    source: job.source,
    freshness: job.freshness,
    category_slug: job.category_slug,
    seniority_level: job.seniority_level,
    industry: job.industry,
  };
}

/** Newest application first. ISO-8601 sorts correctly as a string. */
export function sortEntries(entries: AppliedMap): AppliedEntry[] {
  return Object.values(entries).sort((a, b) => b.applied_at.localeCompare(a.applied_at));
}
