import Link from "next/link";

import { AppliedTile } from "@/components/AppliedTile";
import { JobCard } from "@/components/JobCard";
import { StatTile } from "@/components/StatTile";
import { api } from "@/lib/api";
import { formatNumber } from "@/lib/format";

// Rendered per request, not at build time.
//
// With `revalidate` alone this page was prerendered during `next build`, which meant the
// build could only succeed where the API was already reachable -- impossible inside
// `docker build`, where the api service does not exist yet. The 30s freshness is not
// lost: api.ts sets `next: { revalidate: 30 }` on every fetch, so the data cache still
// collapses repeat calls; only the render moves to request time.
export const dynamic = "force-dynamic";

export default async function HomePage() {
  const [stats, latest, states, categories] = await Promise.all([
    api.stats(),
    api.latest({ limit: 8 }),
    api.stateCounts(),
    api.categories(),
  ]);

  const topStates = states.slice(0, 12);

  return (
    <div className="space-y-10">
      <section>
        <h1 className="text-3xl font-bold tracking-tight">Fresh U.S. Jobs</h1>
        <p className="mt-2 max-w-2xl text-slate-600 dark:text-slate-400">
          {formatNumber(stats.active_jobs)} active jobs from {formatNumber(stats.companies)}{" "}
          companies. Every posting keeps both the employer&apos;s posting date and the moment
          we first detected it.
        </p>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          At a glance
        </h2>
        {/*
          Each tile opens the exact rows it counted, which is why "Found today" travels
          through stats.day_start and status=any: the counter spans every status and is
          measured from the server's midnight, not the browser's.
        */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          <StatTile
            label="Found today"
            value={stats.detected_today}
            tone="fresh"
            href={`/jobs?seen_since=${encodeURIComponent(stats.day_start)}&status=any&sort=first_seen_desc`}
          />
          <StatTile label="Active jobs" value={stats.active_jobs} href="/jobs" />
          <StatTile label="Remote" value={stats.remote_jobs} href="/jobs?remote=REMOTE" />
          <StatTile label="Companies" value={stats.companies} href="/companies" />
          <AppliedTile />
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          By employer posting date
        </h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          {/*
            These can legitimately be zero. The source publishes one batch per day and its
            posted_at values trail the file date, so an empty "last hour" bucket is a
            property of the feed, not a bug — and saying so is better than hiding it.
          */}
          <StatTile
            label="Posted last hour"
            value={stats.posted_last_hour}
            tone={stats.posted_last_hour > 0 ? "fresh" : "muted"}
            note={stats.posted_last_hour === 0 ? "source publishes daily" : undefined}
          />
          <StatTile
            label="Posted last 6h"
            value={stats.posted_last_6h}
            tone={stats.posted_last_6h > 0 ? "fresh" : "muted"}
          />
          <StatTile label="Posted last 24h" value={stats.posted_last_24h} />
          <StatTile
            label="No reliable date"
            value={stats.unknown_posted_at}
            tone="muted"
            note="shown, never guessed"
          />
        </div>
      </section>

      <section>
        <h2 className="mb-4 text-xl font-semibold">Browse by role type</h2>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {categories
            .filter((category) => category.job_count > 0 && category.slug !== "other")
            .slice(0, 12)
            .map((category) => (
              <Link
                key={category.slug}
                href={`/jobs?category=${category.slug}`}
                className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 transition hover:border-blue-400 hover:shadow-sm dark:border-slate-800 dark:bg-slate-900"
              >
                <span aria-hidden className="text-xl">
                  {category.icon}
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">{category.name}</span>
                  <span className="block text-xs text-slate-500">
                    {formatNumber(category.job_count)} jobs
                  </span>
                </span>
              </Link>
            ))}
        </div>
      </section>

      <section>
        <div className="mb-4 flex items-baseline justify-between">
          <h2 className="text-xl font-semibold">Newest to our platform</h2>
          <Link href="/jobs" className="text-sm font-medium text-blue-600 hover:underline">
            Browse all →
          </Link>
        </div>
        <div className="grid gap-3">
          {latest.items.map((job) => (
            <JobCard key={job.id} job={job} />
          ))}
        </div>
      </section>

      <section>
        <h2 className="mb-4 text-xl font-semibold">Jobs by state</h2>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-6">
          {topStates.map((state) => (
            <Link
              key={state.state_code}
              href={`/jobs?state=${state.state_code}`}
              className="rounded-lg border border-slate-200 bg-white px-3 py-2 transition hover:border-blue-400 dark:border-slate-800 dark:bg-slate-900"
            >
              <div className="font-semibold">{state.state_code}</div>
              <div className="text-xs text-slate-500">
                {formatNumber(state.job_count)} jobs
              </div>
            </Link>
          ))}
        </div>
      </section>
    </div>
  );
}
