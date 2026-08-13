"use client";

import type { JobSummary } from "@/lib/api";

import { useApplied } from "./AppliedProvider";

/**
 * Marks a job applied, and offers the way back out.
 *
 * Undo matters more than it looks: marking applied removes the job from every feed, so
 * without it a misclick would hide a job the user never applied to, with no way to find
 * it again short of searching for it by name.
 */
export function AppliedButton({ job, size = "sm" }: { job: JobSummary; size?: "sm" | "lg" }) {
  const { isApplied, apply, unapply } = useApplied();
  const applied = isApplied(job.id);
  const pad = size === "lg" ? "px-5 py-2.5 text-base" : "px-3 py-1.5 text-sm";

  if (applied) {
    return (
      <span className="inline-flex items-center gap-2">
        <span
          className={`inline-flex items-center gap-1.5 rounded-lg border border-emerald-300 bg-emerald-50 font-medium text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-300 ${pad}`}
        >
          <span aria-hidden>✓</span> Applied
        </span>
        <button
          type="button"
          onClick={() => unapply(job.id)}
          className="text-xs text-slate-500 underline-offset-2 hover:text-blue-600 hover:underline"
        >
          Undo
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => apply(job)}
      title="Mark as applied — moves this job to your Applied list"
      className={`rounded-lg border border-emerald-600 font-medium text-emerald-700 transition hover:bg-emerald-600 hover:text-white dark:border-emerald-500 dark:text-emerald-400 dark:hover:bg-emerald-600 dark:hover:text-white ${pad}`}
    >
      Applied
    </button>
  );
}
