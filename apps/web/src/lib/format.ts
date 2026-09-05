/**
 * Presentation helpers.
 *
 * The rules here exist because the spec is explicit that timestamps must never be
 * fabricated. Two of them are load-bearing:
 *
 * 1. A relative time is only ever rendered from a timestamp the platform trusts. When
 *    `posted_at_is_valid` is false the UI says so, rather than inventing "posted recently".
 * 2. "Posted" and "Detected" are always labelled separately, never merged into one line.
 */

import type { JobSummary } from "./api";

export function relativeTime(iso: string | null): string {
  if (!iso) return "unknown";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";

  const seconds = Math.floor((Date.now() - then) / 1000);
  if (seconds < 0) return "in the future";
  if (seconds < 60) return `${seconds}s ago`;

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;

  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;

  const months = Math.floor(days / 30);
  if (months < 12) return `${months} month${months === 1 ? "" : "s"} ago`;
  return `${Math.floor(months / 12)} year${Math.floor(months / 12) === 1 ? "" : "s"} ago`;
}

export function absoluteTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

/**
 * How to render the "posted" line.
 *
 * Returns an explicit marker rather than a string when the source date cannot be trusted,
 * so a caller cannot accidentally print a confident relative time for a job whose date is
 * missing or 34 days in the future.
 */
export function postedLabel(job: Pick<JobSummary, "posted_at" | "posted_at_is_valid">): {
  text: string;
  trusted: boolean;
} {
  if (!job.posted_at) {
    return { text: "Posting date not provided by source", trusted: false };
  }
  if (!job.posted_at_is_valid) {
    return { text: "Posting date reported by source is unreliable", trusted: false };
  }
  return { text: `Posted ${relativeTime(job.posted_at)}`, trusted: true };
}

export function formatSalary(job: JobSummary): string | null {
  const rawMin = job.salary_min === null ? null : Number(job.salary_min);
  const rawMax = job.salary_max === null ? null : Number(job.salary_max);

  // A zero bound carries no information and reads as an error ("$0-$170K"). The source
  // uses it as a placeholder for "unspecified", so treat it as absent.
  const min = rawMin && rawMin > 0 ? rawMin : null;
  const max = rawMax && rawMax > 0 ? rawMax : null;
  if (min === null && max === null) return null;

  const currency = job.salary_currency ?? "USD";
  const perUnit: Record<string, string> = {
    HOURLY: "/hr",
    DAILY: "/day",
    WEEKLY: "/wk",
    MONTHLY: "/mo",
    ANNUAL: "",
    UNKNOWN: "",
  };
  const suffix = perUnit[job.salary_interval] ?? "";

  // Annual figures read better abbreviated; hourly rates must keep their cents.
  // Abbreviate by magnitude. Currencies like JPY run to eight figures, where "13000K"
  // is unreadable and "13M" is not.
  const compact = (value: number): string => {
    if (job.salary_interval !== "ANNUAL") {
      return value % 1 === 0 ? String(value) : value.toFixed(2);
    }
    if (value >= 1_000_000) {
      const millions = value / 1_000_000;
      return `${millions % 1 === 0 ? millions : millions.toFixed(1)}M`;
    }
    if (value >= 1000) return `${Math.round(value / 1000)}K`;
    return String(Math.round(value));
  };

  const symbol = currency === "USD" ? "$" : `${currency} `;
  if (min !== null && max !== null && min !== max) {
    return `${symbol}${compact(min)}–${symbol}${compact(max)}${suffix}`;
  }
  // Only one bound is known. Saying which one it is beats presenting it as a fixed rate.
  if (min === null) return `Up to ${symbol}${compact(max!)}${suffix}`;
  if (max === null) return `From ${symbol}${compact(min)}${suffix}`;
  return `${symbol}${compact(min)}${suffix}`;
}

/** Countries this platform keeps. A code that is not one of them is not labelled. */
const COUNTRY_NAMES: Record<string, string> = { US: "United States", IN: "India" };

export function locationLabel(job: JobSummary): string {
  // Both branches used to say "US" unconditionally, which on the India board turned a
  // remote job in Bengaluru into "Remote (US)" -- a wrong location stated with confidence.
  if (job.remote_type === "REMOTE" && !job.city) {
    return job.country_code ? `Remote (${job.country_code})` : "Remote";
  }
  const parts = [job.city, job.state_code].filter(Boolean);
  if (parts.length === 0) {
    return COUNTRY_NAMES[job.country_code ?? ""] ?? "Location unknown";
  }
  return parts.join(", ");
}

export const REMOTE_LABEL: Record<JobSummary["remote_type"], string> = {
  REMOTE: "Remote",
  HYBRID: "Hybrid",
  ONSITE: "On-site",
  UNKNOWN: "Not stated",
};

export const EMPLOYMENT_LABEL: Record<string, string> = {
  FULL_TIME: "Full-time",
  PART_TIME: "Part-time",
  CONTRACT: "Contract",
  TEMPORARY: "Temporary",
  INTERNSHIP: "Internship",
  VOLUNTEER: "Volunteer",
  OTHER: "Other",
  UNKNOWN: "Not stated",
};

export const FRESHNESS_LABEL: Record<string, { text: string; tone: "fresh" | "warm" | "muted" }> = {
  NEW_LAST_HOUR: { text: "Found in last hour", tone: "fresh" },
  NEW_LAST_6_HOURS: { text: "Found in last 6h", tone: "fresh" },
  NEW_TODAY: { text: "Found today", tone: "fresh" },
  POSTED_LAST_24_HOURS: { text: "Posted in last 24h", tone: "warm" },
  POSTED_TODAY: { text: "Posted today", tone: "warm" },
  UPDATED_TODAY: { text: "Updated today", tone: "warm" },
  OLDER: { text: "", tone: "muted" },
  EXPIRED: { text: "Closed", tone: "muted" },
};

export const SENIORITY_LABEL: Record<string, string> = {
  INTERNSHIP: "Intern",
  ENTRY: "Entry level",
  MID: "Mid level",
  SENIOR: "Senior",
  LEAD: "Lead",
  MANAGER: "Manager",
  DIRECTOR: "Director",
  EXECUTIVE: "Executive",
  UNKNOWN: "Not stated",
};

export function formatNumber(value: number): string {
  return value.toLocaleString("en-US");
}
