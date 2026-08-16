"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, useTransition } from "react";

import { loadNewJobs, getLoadStatus, type LoadState } from "@/app/admin/actions";

/**
 * "Load new jobs" — fetch from the source on demand.
 *
 * The work is queued rather than performed: the API cannot run an ingest itself, and a
 * fetch takes minutes, far longer than a request should be held open. So the button
 * reports a lifecycle rather than a result, and polls while anything is in flight.
 *
 * Polling stops the moment the run settles. A dashboard left open on a second monitor
 * should not spend the afternoon asking a question whose answer stopped changing.
 */

const TONE: Record<string, string> = {
  QUEUED: "text-amber-600 dark:text-amber-400",
  RUNNING: "text-amber-600 dark:text-amber-400",
  SUCCEEDED: "text-emerald-600 dark:text-emerald-400",
  SKIPPED: "text-slate-500",
  FAILED: "text-red-600 dark:text-red-400",
  IDLE: "text-slate-500",
};

const IDLE_LABEL: Record<string, string> = {
  SUCCEEDED: "Last fetch completed",
  SKIPPED: "Last fetch skipped — another ingest was running",
  FAILED: "Last fetch failed",
  IDLE: "No fetch has been requested from here yet",
};

export function LoadJobsButton({ initial }: { initial: LoadState }) {
  const [state, setState] = useState<LoadState>(initial);
  const [pending, startTransition] = useTransition();
  const router = useRouter();

  useEffect(() => {
    if (!state.busy) return;
    const id = setInterval(async () => {
      const next = await getLoadStatus();
      setState(next);
      // A finished run means new rows; refresh so the figures on this page agree with it.
      if (!next.busy) router.refresh();
    }, 5000);
    return () => clearInterval(id);
  }, [state.busy, router]);

  const click = () => {
    startTransition(async () => setState(await loadNewJobs()));
  };

  const busy = state.busy || pending;
  // Disabled during the cooldown as well as while busy, so the button never invites a
  // click the server is going to refuse.
  const cooling = !busy && state.retry_after > 0;
  const label = state.message ?? IDLE_LABEL[state.status] ?? state.status;

  return (
    <div className="flex flex-wrap items-center gap-3">
      <button
        type="button"
        onClick={click}
        disabled={busy || cooling}
        aria-busy={busy}
        className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {busy ? (
          <span
            aria-hidden
            className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white"
          />
        ) : null}
        {busy ? "Loading…" : "Load new jobs"}
      </button>

      <span className={`text-sm ${TONE[state.status] ?? "text-slate-500"}`} role="status">
        {label}
      </span>
    </div>
  );
}
