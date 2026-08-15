import { JobCard } from "@/components/JobCard";
import { requireApprovedPage } from "@/lib/guard";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";

export const revalidate = 30;

type Search = Promise<Record<string, string | string[] | undefined>>;

export default async function SearchPage({ searchParams }: { searchParams: Search }) {
  // Redirects to /login or /pending. The API enforces this independently.
  await requireApprovedPage();
  const params = await searchParams;
  const q = (Array.isArray(params.q) ? params.q[0] : params.q) ?? "";

  const page = q ? await api.search({ q, limit: 20 }) : null;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Search jobs</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Full-text search over title, department, city and description.
        </p>
      </div>

      {/* A plain GET form: search works with JavaScript disabled. */}
      <form action="/search" method="get" className="flex gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q}
          placeholder="e.g. python engineer, nurse, warehouse"
          maxLength={200}
          className="flex-1 rounded-lg border border-slate-300 bg-white px-4 py-2.5 outline-none focus:border-blue-500 dark:border-slate-700 dark:bg-slate-900"
        />
        <button
          type="submit"
          className="rounded-lg bg-blue-600 px-5 py-2.5 font-medium text-white transition hover:bg-blue-700"
        >
          Search
        </button>
      </form>

      {page ? (
        <>
          <p className="text-sm text-slate-500">
            {formatNumber(page.items.length)} result{page.items.length === 1 ? "" : "s"} for
            &ldquo;{q}&rdquo;{page.meta.has_more ? " (more available)" : ""}
          </p>
          <div className="grid gap-3">
            {page.items.map((job) => (
              <JobCard key={job.id} job={job} />
            ))}
          </div>
        </>
      ) : (
        <p className="text-slate-500">Enter a query to search.</p>
      )}
    </div>
  );
}
