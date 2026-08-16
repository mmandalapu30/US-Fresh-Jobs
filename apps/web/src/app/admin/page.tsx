import Link from "next/link";

import { FetchJobsButton } from "@/components/FetchJobsButton";
import { adminApi } from "@/lib/admin";
import { relativeTime } from "@/lib/format";
import { getFetchStatus } from "@/app/admin/actions";
import { requireAdminPage } from "@/lib/guard";

export const dynamic = "force-dynamic";

export const metadata = { title: "Administration · US Fresh Jobs" };

const CARDS = [
  { key: "total_users", label: "Total users", href: "/admin/users", tone: "" },
  { key: "pending", label: "Pending approval", href: "/admin/users/pending", tone: "text-amber-600 dark:text-amber-400" },
  { key: "approved", label: "Approved", href: "/admin/users/approved", tone: "text-emerald-600 dark:text-emerald-400" },
  { key: "rejected", label: "Rejected", href: "/admin/users/rejected", tone: "text-red-600 dark:text-red-400" },
  { key: "suspended", label: "Suspended", href: "/admin/users/suspended", tone: "" },
] as const;

/** Turn an audit verb into something readable without a glossary. */
function describe(action: string): string {
  return (
    {
      ADMIN_APPROVED_USER: "approved",
      ADMIN_REJECTED_USER: "rejected",
      ADMIN_SUSPENDED_USER: "suspended",
      ADMIN_REACTIVATED_USER: "reactivated",
      ADMIN_CHANGED_ROLE: "changed the role of",
    }[action] ?? action.toLowerCase().replaceAll("_", " ")
  );
}

export default async function AdminOverview() {
  // The layout redirects too, but Next renders layouts and pages in parallel --
  // without this the fetch below runs first and throws 401 for a signed-out visitor.
  await requireAdminPage();

  const [summary, audit, fetchState] = await Promise.all([
    adminApi.summary(),
    adminApi.audit(10).catch(() => []),
    getFetchStatus(),
  ]);

  return (
    <div className="space-y-8">
      {summary.pending > 0 ? (
        <Link
          href="/admin/users/pending"
          className="flex items-center gap-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 transition hover:bg-amber-100 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200 dark:hover:bg-amber-900"
        >
          <span className="grid h-6 w-6 place-items-center rounded-full bg-amber-500 text-xs font-bold text-white">
            {summary.pending}
          </span>
          <span>
            New access request{summary.pending === 1 ? "" : "s"} received — review them now
          </span>
        </Link>
      ) : null}

      <section className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
        <div className="mb-3">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
            Job data
          </h2>
          <p className="mt-1 text-xs text-slate-500">
            Jobs are fetched automatically every morning. Use this to pull the latest now.
          </p>
        </div>
        <FetchJobsButton initial={fetchState} />
      </section>

      <section>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          {CARDS.map((c) => (
            <Link
              key={c.key}
              href={c.href}
              className="rounded-xl border border-slate-200 bg-white p-4 transition hover:border-blue-400 dark:border-slate-800 dark:bg-slate-900"
            >
              <div className="text-xs font-medium uppercase tracking-wide text-slate-500">
                {c.label}
              </div>
              <div className={`mt-1 text-2xl font-bold tabular-nums ${c.tone}`}>
                {summary[c.key].toLocaleString()}
              </div>
            </Link>
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Recent administrative actions
        </h2>
        {audit.length === 0 ? (
          <p className="rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-700">
            No administrative actions recorded yet.
          </p>
        ) : (
          <ul className="divide-y divide-slate-200 rounded-xl border border-slate-200 text-sm dark:divide-slate-800 dark:border-slate-800">
            {audit.map((entry) => (
              <li key={entry.id} className="flex flex-wrap items-baseline gap-x-1.5 px-4 py-2.5">
                <span className="font-medium">{entry.admin_email ?? "an administrator"}</span>
                <span className="text-slate-500">{describe(entry.action)}</span>
                <span className="font-medium">{entry.target_email ?? "a user"}</span>
                {entry.previous_status && entry.new_status ? (
                  <span className="text-xs text-slate-400">
                    ({entry.previous_status.toLowerCase()} → {entry.new_status.toLowerCase()})
                  </span>
                ) : null}
                <span className="ml-auto text-xs text-slate-400">
                  {relativeTime(entry.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
