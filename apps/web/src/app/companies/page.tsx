import Link from "next/link";

import { requireApprovedPage } from "@/lib/guard";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";

export const revalidate = 30;

const PAGE_SIZE = 60;

type Search = Promise<Record<string, string | string[] | undefined>>;

/**
 * The employer directory behind the "Companies" stat.
 *
 * It lists every company that has ever posted here, including those whose roles have all
 * expired — they show a zero rather than disappearing. That is what makes the total equal
 * the figure on the home page: a tile that says 1,257 must not open a list of 965.
 */
export default async function CompaniesPage({ searchParams }: { searchParams: Search }) {
  // Redirects to /login or /pending. The API enforces this independently.
  await requireApprovedPage();
  const params = await searchParams;
  const one = (key: string): string | undefined => {
    const value = params[key];
    return Array.isArray(value) ? value[0] : value;
  };

  const q = one("q")?.trim() ?? "";
  const offset = Math.max(0, Number.parseInt(one("offset") ?? "0", 10) || 0);

  const page = await api.companies({ q: q || undefined, limit: PAGE_SIZE, offset });

  const shown = page.items.length;
  const from = shown === 0 ? 0 : offset + 1;
  const to = offset + shown;
  const hasPrev = offset > 0;
  const hasMore = to < page.total;

  const href = (nextOffset: number): string => {
    const next = new URLSearchParams();
    if (q) next.set("q", q);
    if (nextOffset > 0) next.set("offset", String(nextOffset));
    const query = next.toString();
    return query ? `/companies?${query}` : "/companies";
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Companies</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          {formatNumber(page.total)} employer{page.total === 1 ? "" : "s"}
          {q ? (
            <>
              {" "}
              matching <span className="font-medium">{q}</span>
            </>
          ) : (
            " have posted here"
          )}
          . Most actively hiring first.
        </p>
      </div>

      {/* A plain GET form, like the job search: filtering works without client JS. */}
      <form action="/companies" method="get" className="flex gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q}
          placeholder="Find an employer by name"
          maxLength={100}
          className="flex-1 rounded-lg border border-slate-300 bg-white px-4 py-2.5 outline-none focus:border-blue-500 dark:border-slate-700 dark:bg-slate-900"
        />
        <button
          type="submit"
          className="rounded-lg bg-blue-600 px-5 py-2.5 font-medium text-white transition hover:bg-blue-700"
        >
          Search
        </button>
      </form>

      {page.items.length === 0 ? (
        <p className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-slate-500 dark:border-slate-700">
          No employer matches that name.{" "}
          <Link href="/companies" className="text-blue-600 hover:underline">
            Clear
          </Link>
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {page.items.map((company) => (
            <Link
              key={company.id}
              href={`/jobs?company_id=${company.id}`}
              className="group flex flex-col justify-between rounded-xl border border-slate-200 bg-white p-4 transition hover:border-blue-400 hover:shadow-sm dark:border-slate-800 dark:bg-slate-900 dark:hover:border-blue-600"
            >
              <div className="min-w-0">
                <div className="truncate font-medium group-hover:text-blue-700 dark:group-hover:text-blue-400">
                  {company.name}
                </div>
                {company.industry ? (
                  <div className="mt-0.5 truncate text-xs text-slate-500">{company.industry}</div>
                ) : null}
              </div>
              <div className="mt-3 flex items-baseline gap-2 text-sm">
                {company.active_job_count > 0 ? (
                  <span className="font-semibold tabular-nums text-emerald-700 dark:text-emerald-400">
                    {formatNumber(company.active_job_count)} open
                  </span>
                ) : (
                  <span className="text-slate-500">No open roles</span>
                )}
                {company.total_job_count > company.active_job_count ? (
                  <span className="text-xs text-slate-500">
                    · {formatNumber(company.total_job_count)} all time
                  </span>
                ) : null}
              </div>
            </Link>
          ))}
        </div>
      )}

      {shown > 0 ? (
        <div className="flex items-center justify-between border-t border-slate-200 pt-4 text-sm dark:border-slate-800">
          <span className="text-slate-500">
            {formatNumber(from)}–{formatNumber(to)} of {formatNumber(page.total)}
          </span>
          <div className="flex gap-2">
            {hasPrev ? (
              <Link
                href={href(Math.max(0, offset - PAGE_SIZE))}
                className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-medium transition hover:border-blue-400 dark:border-slate-700 dark:bg-slate-900"
              >
                ← Previous
              </Link>
            ) : null}
            {hasMore ? (
              <Link
                href={href(offset + PAGE_SIZE)}
                className="rounded-lg border border-slate-300 bg-white px-4 py-2 font-medium transition hover:border-blue-400 dark:border-slate-700 dark:bg-slate-900"
              >
                Next →
              </Link>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
