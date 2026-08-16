"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

import { requestFetch, getFetchStatus, type FetchState } from "@/app/admin/actions";

/**
 * "Fetch jobs now" — an on-demand ingest, triggered from the console.
 *
 * The work is queued rather than performed: the API cannot run an ingest itself, and a
 * fetch takes minutes, which is far longer than a request should be held open. So the
 * button reports a lifecycle rather than a result, and polls while anything is in flight.
 *
 * Polling stops the moment the run settles. A console left open on a second monitor should
 * not spend the afternoon asking a question whose answer has stopped changing.
 */

const TONE: Record<string, string> = {
  QUEUED: "text-amber-600 dark:text-amber-400",
  RUNNING: "text-amber-600 dark:text-amber-400",
  SUCCEEDED: "text-emerald-600 dark:text-emerald-400",
  SKIPPED: "text-slate-500",
  FAILED: "text-red-600 dark:text-red-400",
  IDLE: "text-slate-500",
};

const LABEL: Record<string, string> = {
  QUEUED: "Queued — starting shortly",
  RUNNING: "Fetching from the source…",
  SUCCEEDED: "Last fetch completed",
  SKIPPED: "Last fetch skipped",
  FAILED: "Last fetch failed",
  IDLE: "No fetch has been run from here yet",
};

export function FetchJobsButton({ initial }: { initial: FetchState }) {
  const [state, setState] = useState<FetchState>(initial);
  const [pending, startTransition] = useTransition();
  const router = useRouter();

  useEffect(() => {
    if (!state.busy) return;
    const id = setInterval(async () => {
      const next = await getFetchStatus();
      setState(next);
      // A finished run means new rows; refresh so the counts on this page agree with it.
      if (!next.busy) router.refresh();
    }, 5000);
    return () => clearInterval(id);
  }, [state.busy, router]);

  const trigger = () => {
    startTransition(async () => {
      setState(await requestFetch());
    });
  };

  const busy = state.busy || pending;

  return (
    <div className="flex flex-wrap items-center gap-3">
      <button
        type="button"
        onClick={trigger}
        disabled={busy}
        aria-busy={busy}
        className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
      >
        {busy ? (
          <span
            aria-hidden
            className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white"
          />
        ) : null}
        {busy ? "Fetching…" : "Fetch jobs now"}
      </button>

      <span className={`text-sm ${TONE[state.status] ?? "text-slate-500"}`}>
        {state.message ?? LABEL[state.status] ?? state.status}
      </span>
    </div>
  );
}
