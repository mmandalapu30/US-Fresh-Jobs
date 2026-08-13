import Link from "next/link";

import type { CategoryFacet, IndustryFacet, SeniorityFacet } from "@/lib/api";
import { SENIORITY_LABEL, formatNumber } from "@/lib/format";

/**
 * Role and level filters.
 *
 * Server-rendered links rather than client state, so filtering works without JavaScript
 * and every filtered view is a real, shareable URL.
 *
 * Multi-select is expressed as repeated query parameters (`?category=a&category=b`),
 * matching what the API accepts, so a URL can always be reasoned about directly.
 */
export function CategoryFilter({
  categories,
  seniorities,
  industries,
  selectedCategories,
  selectedSeniorities,
  selectedIndustries,
  buildHref,
}: {
  categories: CategoryFacet[];
  seniorities: SeniorityFacet[];
  industries: IndustryFacet[];
  selectedCategories: string[];
  selectedSeniorities: string[];
  selectedIndustries: string[];
  buildHref: (patch: {
    category?: string[];
    seniority?: string[];
    industry?: string[];
  }) => string;
}) {
  const toggle = (list: string[], value: string): string[] =>
    list.includes(value) ? list.filter((item) => item !== value) : [...list, value];

  // Empty categories are hidden: a chip reading "(0)" is noise, not information.
  const visible = categories.filter((c) => c.job_count > 0);
  const levels = seniorities.filter((s) => s.job_count > 0 && s.level !== "UNKNOWN");

  return (
    <div className="space-y-4">
      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Role type
          </h2>
          {selectedCategories.length > 0 ? (
            <Link
              href={buildHref({ category: [] })}
              className="text-xs text-blue-600 hover:underline"
            >
              Clear
            </Link>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2">
          {visible.map((category) => {
            const active = selectedCategories.includes(category.slug);
            return (
              <Link
                key={category.slug}
                href={buildHref({ category: toggle(selectedCategories, category.slug) })}
                aria-pressed={active}
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition ${
                  active
                    ? "border-blue-600 bg-blue-600 text-white"
                    : "border-slate-300 bg-white text-slate-700 hover:border-blue-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
                }`}
              >
                <span aria-hidden>{category.icon}</span>
                <span>{category.name}</span>
                <span
                  className={`tabular-nums text-xs ${
                    active ? "text-blue-100" : "text-slate-500"
                  }`}
                >
                  {formatNumber(category.job_count)}
                </span>
              </Link>
            );
          })}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Level
          </h2>
          {selectedSeniorities.length > 0 ? (
            <Link
              href={buildHref({ seniority: [] })}
              className="text-xs text-blue-600 hover:underline"
            >
              Clear
            </Link>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2">
          {levels.map((item) => {
            const active = selectedSeniorities.includes(item.level);
            return (
              <Link
                key={item.level}
                href={buildHref({ seniority: toggle(selectedSeniorities, item.level) })}
                aria-pressed={active}
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition ${
                  active
                    ? "border-emerald-600 bg-emerald-600 text-white"
                    : "border-slate-300 bg-white text-slate-700 hover:border-emerald-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
                }`}
              >
                <span>{SENIORITY_LABEL[item.level] ?? item.level}</span>
                <span
                  className={`tabular-nums text-xs ${
                    active ? "text-emerald-100" : "text-slate-500"
                  }`}
                >
                  {formatNumber(item.job_count)}
                </span>
              </Link>
            );
          })}
        </div>
        {/*
          Level is derived from the title, and most titles simply do not state one.
          Saying so is better than letting a user assume the filter is broken.
        */}
        <p className="mt-2 text-xs text-slate-500">
          Level is inferred from the job title. Roles whose title states no level are not
          shown under a level filter.
        </p>
      </section>

      <section>
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Industry
          </h2>
          {selectedIndustries.length > 0 ? (
            <Link
              href={buildHref({ industry: [] })}
              className="text-xs text-blue-600 hover:underline"
            >
              Clear
            </Link>
          ) : null}
        </div>

        <div className="flex flex-wrap gap-2">
          {industries.slice(0, 14).map((item) => {
            const active = selectedIndustries.includes(item.industry);
            return (
              <Link
                key={item.industry}
                href={buildHref({ industry: toggle(selectedIndustries, item.industry) })}
                aria-pressed={active}
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition ${
                  active
                    ? "border-violet-600 bg-violet-600 text-white"
                    : "border-slate-300 bg-white text-slate-700 hover:border-violet-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
                }`}
              >
                <span>{item.industry}</span>
                <span
                  className={`tabular-nums text-xs ${
                    active ? "text-violet-100" : "text-slate-500"
                  }`}
                >
                  {formatNumber(item.job_count)}
                </span>
              </Link>
            );
          })}
        </div>
        {/*
          Industry describes the EMPLOYER; category describes the JOB. A nurse at a school
          is healthcare-in-education, and users filter on both axes independently.
        */}
        <p className="mt-2 text-xs text-slate-500">
          Industry is the employer&apos;s sector, reported by the source. Role type above
          describes the job itself.
        </p>
      </section>
    </div>
  );
}
