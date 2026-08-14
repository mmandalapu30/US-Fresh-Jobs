import { api } from "@/lib/api";
import { absoluteTime, relativeTime } from "@/lib/format";

/**
 * When the board was last filled, shown on every page.
 *
 * A platform whose promise is freshness has to answer "how current is this?" without the
 * visitor having to trust that something ran. The relative time is the headline because
 * that is the question people actually have; the absolute time is in the tooltip for
 * anyone who needs to be exact about it.
 *
 * Deliberately reads the most recent SUCCEEDED run, not the most recent run: a failed or
 * still-running pass has delivered nothing, and reporting it as an update would make a
 * stale board look current — the exact failure this strip exists to prevent.
 *
 * Server-rendered on every request, so it can never show a cached time from an earlier
 * page load. If the stats call fails the strip renders nothing rather than blocking the
 * page: freshness is context, not content.
 */
export async function DataFreshness() {
  let stats;
  try {
    stats = await api.stats();
  } catch {
    return null;
  }

  const running = stats.ingest_running > 0;

  // No successful run yet -- a fresh deployment before its first ingest finishes. Saying
  // so is better than an empty space that reads as "nothing to report".
  if (!stats.last_ingest_at) {
    return (
      <div className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
        <div className="mx-auto flex max-w-6xl items-center gap-2 px-4 py-1.5 text-xs text-slate-500">
          <span
            aria-hidden
            className={`h-1.5 w-1.5 rounded-full ${running ? "animate-pulse bg-amber-500" : "bg-slate-400"}`}
          />
          <span>{running ? "First update in progress…" : "No completed update yet"}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="border-b border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900/60">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-2 gap-y-1 px-4 py-1.5 text-xs text-slate-500">
        <span
          aria-hidden
          className={`h-1.5 w-1.5 rounded-full ${running ? "animate-pulse bg-amber-500" : "bg-emerald-500"}`}
        />
        <span>
          Jobs last updated{" "}
          <time
            dateTime={stats.last_ingest_at}
            title={absoluteTime(stats.last_ingest_at)}
            className="font-medium text-slate-700 dark:text-slate-300"
          >
            {relativeTime(stats.last_ingest_at)}
          </time>
        </span>
        {running ? (
          <span className="text-amber-600 dark:text-amber-400">· update running now</span>
        ) : null}
        <span aria-hidden className="text-slate-300 dark:text-slate-700">
          ·
        </span>
        {/*
          Active, not detected_today. The detection counter spans every status, so after
          a backfill it read 42,625 while the board actually held 6,132 -- accurate and
          misleading at the same time. This number matches what clicking through shows.
        */}
        <span>{stats.active_jobs.toLocaleString()} active jobs</span>
      </div>
    </div>
  );
}
