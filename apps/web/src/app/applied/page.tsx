"use client";

import Link from "next/link";

import { useApplied } from "@/components/AppliedProvider";
import { JobCard } from "@/components/JobCard";
import { sortEntries } from "@/lib/applied";
import { formatNumber, relativeTime } from "@/lib/format";

/**
 * Jobs this browser has marked applied.
 *
 * Rendered entirely from local state — the list is a map keyed by job id, so a job cannot
 * appear twice however many times its button was pressed, and each card is drawn from the
 * snapshot taken at the moment of applying rather than refetched. That is deliberate: a
 * job that has since expired would no longer come back from the feed API, and losing your
 * own application record because the employer closed the posting would be the wrong
 * behaviour entirely.
 */
export default function AppliedPage() {
  const { entries, ready } = useApplied();
  const applications = sortEntries(entries);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Applied</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          {ready
            ? `${formatNumber(applications.length)} job${applications.length === 1 ? "" : "s"} you have marked as applied. They are hidden from the feeds so you do not see them twice.`
            : "Reading your applications…"}
        </p>
      </div>

      {ready && applications.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 p-10 text-center dark:border-slate-700">
          <p className="text-slate-600 dark:text-slate-400">
            Nothing here yet. Press <span className="font-medium">Applied</span> on any job
            card and it moves here.
          </p>
          <Link
            href="/jobs"
            className="mt-4 inline-block rounded-lg bg-blue-600 px-5 py-2 text-sm font-medium text-white transition hover:bg-blue-700"
          >
            Browse jobs
          </Link>
        </div>
      ) : null}

      <div className="grid gap-3">
        {applications.map((entry) => (
          <div key={entry.job.id} className="space-y-1">
            <div className="px-1 text-xs font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
              Applied {relativeTime(entry.applied_at)}
            </div>
            {/* hideWhenApplied={false} — every card here is applied by definition. */}
            <JobCard job={entry.job} hideWhenApplied={false} />
          </div>
        ))}
      </div>

      {ready && applications.length > 0 ? (
        <p className="text-xs text-slate-500">
          This list lives in this browser only — there are no accounts yet, so it does not
          follow you to another device, and clearing site data clears it. Job details are
          shown as they were when you applied.
        </p>
      ) : null}
    </div>
  );
}
