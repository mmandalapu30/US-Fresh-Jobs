import { getLoadStatus } from "@/app/admin/actions";
import { LoadJobsButton } from "@/components/LoadJobsButton";
import { api } from "@/lib/api";
import { currentCountry } from "@/lib/country.server";
import { absoluteTime, formatNumber } from "@/lib/format";

// Rendered per request, not at build time.
//
// With `revalidate` alone this page was prerendered during `next build`, so the build
// only succeeded where the API was already reachable -- impossible inside `docker build`,
// where the api service does not exist yet. Freshness is unchanged: api.ts sets
// `next: { revalidate: 30 }` per fetch, so the data cache still collapses repeat calls.
export const dynamic = "force-dynamic";

interface Run {
  source: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  files_processed: number;
  rows_processed: number;
  rows_accepted: number;
  rows_rejected: number;
  rows_inserted: number;
  rows_updated: number;
  duplicates_found: number;
}

interface Rejection {
  reason: string;
  n: number;
}

export default async function AdminPage() {
  // The dashboard reports on the board you are looking at: /stats is country-scoped now,
  // so an unscoped call here would have silently reported US numbers under the India switch.
  const country = await currentCountry();

  const [stats, health, loadState] = await Promise.all([
    api.stats({ country }),
    api.ingestion(),
    getLoadStatus(),
  ]);
  const runs = health.runs as Run[];
  const rejections = health.rejections as Rejection[];

  return (
    <div className="space-y-8">
      <section className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Job data
        </h2>
        <p className="mt-1 mb-3 text-xs text-slate-500">
          Jobs load automatically each morning, with catch-up runs through the afternoon.
          Use this to pull the latest right now.
        </p>
        <LoadJobsButton initial={loadState} />
      </section>
      <h1 className="text-2xl font-bold">Admin · ingestion</h1>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Inventory
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {[
            ["Total jobs", stats.total_jobs],
            ["Active", stats.active_jobs],
            ["Expired", stats.expired_jobs],
            ["Companies", stats.companies],
            ["Detected today", stats.detected_today],
            ["Updated today", stats.updated_today],
            ["Remote", stats.remote_jobs],
            ["No reliable date", stats.unknown_posted_at],
          ].map(([label, value]) => (
            <div
              key={label as string}
              className="rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900"
            >
              <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
              <div className="mt-1 text-xl font-semibold tabular-nums">
                {formatNumber(value as number)}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Latest sync run per source
        </h2>
        <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
          <table className="min-w-full text-sm">
            <thead className="bg-slate-100 text-left dark:bg-slate-800">
              <tr>
                {["Source", "Status", "Started", "Duration", "Files", "Processed", "Accepted", "Rejected", "Inserted", "Updated"].map(
                  (header) => (
                    <th key={header} className="px-3 py-2 font-medium">
                      {header}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
              {runs.map((run) => (
                <tr key={run.source} className="bg-white dark:bg-slate-900">
                  <td className="px-3 py-2 font-medium">{run.source}</td>
                  <td className="px-3 py-2">
                    <span
                      className={
                        run.status === "SUCCEEDED"
                          ? "rounded bg-emerald-100 px-2 py-0.5 text-xs text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                          : "rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800 dark:bg-amber-950 dark:text-amber-300"
                      }
                    >
                      {run.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-slate-500">{absoluteTime(run.started_at)}</td>
                  <td className="px-3 py-2 tabular-nums">
                    {run.duration_seconds ? `${Math.round(run.duration_seconds)}s` : "—"}
                  </td>
                  <td className="px-3 py-2 tabular-nums">{run.files_processed}</td>
                  <td className="px-3 py-2 tabular-nums">{formatNumber(run.rows_processed)}</td>
                  <td className="px-3 py-2 tabular-nums">{formatNumber(run.rows_accepted)}</td>
                  <td className="px-3 py-2 tabular-nums">{formatNumber(run.rows_rejected)}</td>
                  <td className="px-3 py-2 tabular-nums">{formatNumber(run.rows_inserted)}</td>
                  <td className="px-3 py-2 tabular-nums">{formatNumber(run.rows_updated)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Rejections by reason
        </h2>
        <p className="mb-3 text-xs text-slate-500">
          Every row the pipeline refused is stored with a reason. Nothing is silently dropped.
        </p>
        <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3">
          {rejections.map((rejection) => (
            <div
              key={rejection.reason}
              className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-3 py-2 dark:border-slate-800 dark:bg-slate-900"
            >
              <span className="text-xs font-medium text-slate-600 dark:text-slate-400">
                {rejection.reason}
              </span>
              <span className="tabular-nums font-semibold">{formatNumber(rejection.n)}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
