import Link from "next/link";

import { AppliedButton } from "@/components/AppliedButton";
import { AppliedGate } from "@/components/AppliedGate";
import type { JobSummary } from "@/lib/api";
import {
  EMPLOYMENT_LABEL,
  FRESHNESS_LABEL,
  REMOTE_LABEL,
  SENIORITY_LABEL,
  formatSalary,
  locationLabel,
  postedLabel,
  relativeTime,
} from "@/lib/format";

const TONE: Record<string, string> = {
  fresh: "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  warm: "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300",
  muted: "bg-slate-200 text-slate-600 dark:bg-slate-800 dark:text-slate-400",
};

export function JobCard({
  job,
  hideWhenApplied = true,
  categoryHref,
}: {
  job: JobSummary;
  /** False on the applied list, which would otherwise hide every card it renders. */
  hideWhenApplied?: boolean;
  /**
   * How the category chip should link. The default discards any filters already applied,
   * which is right on the home page and on search -- there is nothing to keep -- but wrong
   * on /jobs, where it silently dropped the state, remote, sort and seen_since the visitor
   * had chosen. That page passes a builder that preserves them.
   */
  categoryHref?: (slug: string) => string;
}) {
  const salary = formatSalary(job);
  const posted = postedLabel(job);
  const freshness = FRESHNESS_LABEL[job.freshness];

  return (
    <AppliedGate jobId={job.id} enabled={hideWhenApplied}>
      <article className="group rounded-xl border border-slate-200 bg-white p-5 transition hover:border-blue-400 hover:shadow-md dark:border-slate-800 dark:bg-slate-900 dark:hover:border-blue-600">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <Link href={`/jobs/${job.id}`} className="block">
              <h2 className="truncate text-base font-semibold text-slate-900 group-hover:text-blue-700 dark:text-slate-100 dark:group-hover:text-blue-400">
                {job.title}
              </h2>
            </Link>
            <p className="mt-0.5 truncate text-sm text-slate-600 dark:text-slate-400">
              {job.company_name ?? "Company not stated"}
            </p>
          </div>

          {freshness?.text ? (
            <span
              className={`shrink-0 rounded-full px-2.5 py-1 text-xs font-medium ${TONE[freshness.tone]}`}
            >
              {freshness.text}
            </span>
          ) : null}
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm text-slate-600 dark:text-slate-400">
          <span>{locationLabel(job)}</span>
          <span aria-hidden className="text-slate-300 dark:text-slate-700">
            ·
          </span>
          <span
            className={
              job.remote_type === "REMOTE"
                ? "font-medium text-emerald-700 dark:text-emerald-400"
                : ""
            }
          >
            {REMOTE_LABEL[job.remote_type]}
          </span>
          {job.seniority_level !== "UNKNOWN" ? (
            <>
              <span aria-hidden className="text-slate-300 dark:text-slate-700">
                ·
              </span>
              <span className="font-medium text-slate-700 dark:text-slate-300">
                {SENIORITY_LABEL[job.seniority_level] ?? job.seniority_level}
              </span>
            </>
          ) : null}
          {job.employment_type !== "UNKNOWN" ? (
            <>
              <span aria-hidden className="text-slate-300 dark:text-slate-700">
                ·
              </span>
              <span>{EMPLOYMENT_LABEL[job.employment_type] ?? job.employment_type}</span>
            </>
          ) : null}
          {salary ? (
            <>
              <span aria-hidden className="text-slate-300 dark:text-slate-700">
                ·
              </span>
              <span className="font-medium text-slate-800 dark:text-slate-200">{salary}</span>
            </>
          ) : null}
        </div>

        <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3 dark:border-slate-800">
          <div className="text-xs text-slate-500 dark:text-slate-500">
            {/*
              Two separate facts, never merged. When the source's date cannot be trusted the
              UI says so instead of substituting our own detection time — that substitution
              is exactly what the spec forbids.
            */}
            <span className={posted.trusted ? "" : "italic"}>{posted.text}</span>
            <span aria-hidden className="mx-1.5 text-slate-300 dark:text-slate-700">
              ·
            </span>
            <span>Found by us {relativeTime(job.first_seen_at)}</span>
          </div>

          <div className="flex items-center gap-2">
            {job.category_slug && job.category_slug !== "other" ? (
              <Link
                href={
                  categoryHref
                    ? categoryHref(job.category_slug)
                    : `/jobs?category=${job.category_slug}`
                }
                className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600 transition hover:bg-blue-100 hover:text-blue-700 dark:bg-slate-800 dark:text-slate-400 dark:hover:bg-blue-950"
              >
                {job.category_slug}
              </Link>
            ) : null}
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] uppercase tracking-wide text-slate-500 dark:bg-slate-800 dark:text-slate-400">
              {job.source}
            </span>
            <Link
              href={`/jobs/${job.id}`}
              className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700"
            >
              View job
            </Link>
            <AppliedButton job={job} />
          </div>
        </div>
      </article>
    </AppliedGate>
  );
}
