import Link from "next/link";

/** Filter chips. Server-rendered links, so filtering works without client JS. */
export function Filters({
  current,
  states,
  extraParams = {},
}: {
  current: Record<string, string | undefined>;
  states: { state_code: string; job_count: number }[];
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
