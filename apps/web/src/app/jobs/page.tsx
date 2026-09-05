import Link from "next/link";

import { Filters } from "@/components/Filters";
import { JobCard } from "@/components/JobCard";
import { api } from "@/lib/api";
import { currentCountry } from "@/lib/country.server";
import { SENIORITY_LABEL, formatNumber } from "@/lib/format";

export const revalidate = 30;

type Search = Promise<Record<string, string | string[] | undefined>>;

/**
 * Single-valued params that every link on this page must preserve.
 *
 * The stat tiles and the employer directory link straight into a filtered feed, so
 * `company_id`, `seen_since` and `status` have to survive a chip toggle or a page turn.
 * Dropping one would quietly widen the result set the user is looking at.
 */
const CARRIED = ["state", "remote", "sort", "q", "company_id", "seen_since", "status"] as const;

type HrefPatch = Partial<Record<(typeof CARRIED)[number], string>> & {
  category?: string[];
  seniority?: string[];
  industry?: string[];
};

export default async function JobsPage({ searchParams }: { searchParams: Search }) {
  const params = await searchParams;

  const one = (key: string): string | undefined => {
    const value = params[key];
    return Array.isArray(value) ? value[0] : value;
  };
  // Multi-select filters arrive as repeated params; a single value is still a list of one.
  const many = (key: string): string[] => {
    const value = params[key];
    if (value === undefined) return [];
    return Array.isArray(value) ? value : [value];
  };

  const current = {
    state: one("state"),
    remote: one("remote"),
    sort: one("sort") ?? "first_seen_desc",
    q: one("q"),
    cursor: one("cursor"),
    company_id: one("company_id"),
    seen_since: one("seen_since"),
    status: one("status"),
  };
  const selectedCategories = many("category");
  const selectedSeniorities = many("seniority");
  const selectedIndustries = many("industry");

  const country = await currentCountry();

  const [page, states, categories] = await Promise.all([
    api.jobs({
      country,
      state: current.state,
      remote: current.remote,
      sort: current.sort,
      q: current.q,
      cursor: current.cursor,
      company_id: current.company_id,
      seen_since: current.seen_since,
      status: current.status,
      category: selectedCategories,
      seniority: selectedSeniorities,
      industry: selectedIndustries,
      limit: 20,
    }),
    api.stateCounts({ country }),
    // Only the category names are still needed, to render the active-filter line. The
    // seniority and industry counts went with the chip panel -- two fewer API calls on
    // every page load.
    api.categories({ country, state: current.state, remote: current.remote }),
  ]);

  /** Build a URL preserving current filters, with a patch applied. */
  const buildHref = (patch: HrefPatch): string => {
    const next = new URLSearchParams();
    for (const key of CARRIED) {
      const value = patch[key] ?? current[key];
      if (value) next.set(key, value);
    }
    for (const value of patch.category ?? selectedCategories) next.append("category", value);
    for (const value of patch.seniority ?? selectedSeniorities) next.append("seniority", value);
    for (const value of patch.industry ?? selectedIndustries) next.append("industry", value);
    // A cursor belongs to one exact filter set; carrying it across a change would page
    // into a different result set.
    const query = next.toString();
    return query ? `/jobs?${query}` : "/jobs";
  };

  const nextHref = (() => {
    if (!page.meta.next_cursor) return null;
    const next = new URLSearchParams();
    for (const key of CARRIED) {
      const value = current[key];
      if (value) next.set(key, value);
    }
    for (const value of selectedCategories) next.append("category", value);
    for (const value of selectedSeniorities) next.append("seniority", value);
    for (const value of selectedIndustries) next.append("industry", value);
    next.set("cursor", page.meta.next_cursor);
    return `/jobs?${next.toString()}`;
  })();

  // Name the surface the visitor actually arrived at. A stat tile that opens a page
  // headed "Browse jobs" gives no clue why the list is narrowed.
  const companyName = current.company_id ? (page.items[0]?.company_name ?? null) : null;
  const heading = current.company_id
    ? (companyName ?? "Jobs at this employer")
    : current.seen_since
      ? "Found today"
      : "Browse jobs";

  const activeLabels = [
    ...selectedCategories.map(
      (slug) => categories.find((c) => c.slug === slug)?.name ?? slug,
    ),
    ...selectedSeniorities.map((level) => SENIORITY_LABEL[level] ?? level),
    ...selectedIndustries,
  ];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">{heading}</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          {current.seen_since ? (
            <>
              Everything this platform first detected today
              {current.status === "any" ? ", including roles that have since expired" : ""}.
            </>
          ) : activeLabels.length > 0 ? (
            <>
              Filtered by <span className="font-medium">{activeLabels.join(" · ")}</span>
              {current.state ? ` in ${current.state}` : ""}
            </>
          ) : (
            "Newest first. Narrow by location, remote or role using the controls below."
          )}
        </p>
        {current.company_id ||
        current.seen_since ||
        current.status ||
        activeLabels.length > 0 ? (
          <Link href="/jobs" className="mt-2 inline-block text-sm text-blue-600 hover:underline">
            ← All jobs
          </Link>
        ) : null}
      </div>

      <Filters
        current={current}
        states={states}
        seniorities={selectedSeniorities}
        extraParams={{
          category: selectedCategories,
          industry: selectedIndustries,
        }}
      />

      <div className="grid gap-3">
        {page.items.length === 0 ? (
          <p className="rounded-xl border border-dashed border-slate-300 p-8 text-center text-slate-500 dark:border-slate-700">
            No jobs match these filters.{" "}
            {/*
              An employer with no open roles is a normal state here, not an error — their
              past postings are kept rather than deleted, so offer them instead of a dead end.
            */}
            {current.company_id && current.status !== "any" ? (
              <Link href={buildHref({ status: "any" })} className="text-blue-600 hover:underline">
                Show this employer&apos;s expired roles
              </Link>
            ) : (
              <Link href="/jobs" className="text-blue-600 hover:underline">
                Clear all
              </Link>
            )}
          </p>
        ) : (
          page.items.map((job) => (
            <JobCard
              key={job.id}
              job={job}
              categoryHref={(slug) => buildHref({ category: [slug] })}
            />
          ))
        )}
      </div>

      {nextHref ? (
        <div className="flex justify-center pt-2">
          <Link
            href={nextHref}
            className="rounded-lg border border-slate-300 bg-white px-5 py-2 text-sm font-medium transition hover:border-blue-400 dark:border-slate-700 dark:bg-slate-900"
          >
            Next {formatNumber(page.meta.page_size)} →
          </Link>
        </div>
      ) : null}
    </div>
  );
}
