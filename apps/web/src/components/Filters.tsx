import Link from "next/link";

import { SENIORITY_LABEL } from "@/lib/format";

/**
 * The ladder, in order, minus UNKNOWN. "Not stated" is the single largest bucket on this
 * source, so offering it as a chip would read as a seniority rather than as the absence
 * of one -- and filtering to it is not a thing anyone wants to do deliberately.
 */
const SENIORITY_ORDER = [
  "INTERNSHIP",
  "ENTRY",
  "MID",
  "SENIOR",
  "LEAD",
  "MANAGER",
  "DIRECTOR",
  "EXECUTIVE",
] as const;

/** Filter chips. Server-rendered links, so filtering works without client JS. */
export function Filters({
  current,
  states,
  seniorities = [],
  extraParams = {},
}: {
  current: Record<string, string | undefined>;
  states: { state_code: string; job_count: number }[];
  /** Seniority levels currently applied. Repeatable, so its chips toggle rather than replace. */
  seniorities?: string[];
  /** Repeatable params this component does not own but must not discard. */
  extraParams?: Record<string, string[]>;
}) {
  const buildHref = (patch: Record<string, string | undefined>): string => {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries({ ...current, ...patch })) {
      if (value) params.set(key, value);
    }
    for (const [key, values] of Object.entries(extraParams)) {
      for (const value of values) params.append(key, value);
    }
    // A cursor belongs to a specific filter set; keeping it across a filter change would
    // page into the wrong result set.
    params.delete("cursor");
    const query = params.toString();
    return query ? `/jobs?${query}` : "/jobs";
  };

  // Seniority is repeatable, so a chip adds or removes its own value and leaves the rest
  // of the selection alone. The single-valued chips above replace their parameter outright;
  // doing that here would make the levels mutually exclusive, which is the opposite of what
  // a job seeker wants -- "senior or lead" is one search, not two.
  const buildSeniorityHref = (value: string | null): string => {
    const params = new URLSearchParams();
    for (const [key, entry] of Object.entries(current)) {
      if (entry) params.set(key, entry);
    }
    for (const [key, values] of Object.entries(extraParams)) {
      for (const entry of values) params.append(key, entry);
    }
    const next =
      value === null
        ? []
        : seniorities.includes(value)
          ? seniorities.filter((level) => level !== value)
          : [...seniorities, value];
    for (const level of next) params.append("seniority", level);
    params.delete("cursor");
    const query = params.toString();
    return query ? `/jobs?${query}` : "/jobs";
  };

  const chip = (active: boolean) =>
    `rounded-full border px-3 py-1 text-sm transition ${
      active
        ? "border-blue-600 bg-blue-600 text-white"
        : "border-slate-300 bg-white text-slate-700 hover:border-blue-400 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300"
    }`;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Work</span>
        <Link href={buildHref({ remote: undefined })} className={chip(!current.remote)}>
          Any
        </Link>
        {["REMOTE", "HYBRID", "ONSITE"].map((value) => (
          <Link
            key={value}
            href={buildHref({ remote: value })}
            className={chip(current.remote === value)}
          >
            {value === "REMOTE" ? "Remote" : value === "HYBRID" ? "Hybrid" : "On-site"}
          </Link>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Sort</span>
        {[
          ["first_seen_desc", "Newest found"],
          ["posted_at_desc", "Newest posted"],
          ["salary_desc", "Highest salary"],
        ].map(([value, label]) => (
          <Link
            key={value}
            href={buildHref({ sort: value })}
            className={chip((current.sort ?? "first_seen_desc") === value)}
          >
            {label}
          </Link>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">Level</span>
        <Link href={buildSeniorityHref(null)} className={chip(seniorities.length === 0)}>
          Any
        </Link>
        {SENIORITY_ORDER.map((value) => (
          <Link
            key={value}
            href={buildSeniorityHref(value)}
            className={chip(seniorities.includes(value))}
          >
            {SENIORITY_LABEL[value] ?? value}
          </Link>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">State</span>
        <Link href={buildHref({ state: undefined })} className={chip(!current.state)}>
          All
        </Link>
        {states.slice(0, 14).map((state) => (
          <Link
            key={state.state_code}
            href={buildHref({ state: state.state_code })}
            className={chip(current.state === state.state_code)}
          >
            {state.state_code}
          </Link>
        ))}
      </div>
    </div>
  );
}
