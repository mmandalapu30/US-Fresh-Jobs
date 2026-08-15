import Link from "next/link";

import { getCurrentUser, isApproved } from "@/lib/session";
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

/**
 * The role families this deployment keeps, in the order they are shown.
 *
 * INGEST_CATEGORY_ALLOWLIST already guarantees nothing else is stored, so this is
 * about presentation order, not filtering -- but naming them here means the headline
 * tiles stay meaningful if the allowlist ever widens, rather than silently showing
 * whichever categories happen to sort first.
 */
const ROLES = ["software", "data-engineering"] as const;

export default async function HomePage() {
  // The landing page is public, so it must render for someone with no session at all.
  // Counts and categories are public; the job feed is not, and asking for it anonymously
  // now returns 401. Fetch it only for those allowed to see it, and let a failure fall
  // back to the signed-out view rather than breaking the page.
  const user = await getCurrentUser();
  const approved = isApproved(user);

  const [stats, states, categories] = await Promise.all([
    api.stats(),
    api.stateCounts(),
    api.categories(),
  ]);
  const latest = approved
    ? await api.latest({ limit: 8 }).catch(() => null)
    : null;

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
          Every tile counts ACTIVE jobs only, and opens exactly the rows it counted.
          The two role tiles come from the category facets rather than a separate query,
          so the headline figure and the filtered page can never disagree.

          There is deliberately no "found today" tile. It counted every status, so on a
          day with a large backfill it read 42,625 while the board held 6,132 -- a number
          that was true and still told the visitor the wrong thing.
        */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
          {ROLES.map((slug) => {
            const role = categories.find((c) => c.slug === slug);
            if (!role) return null;
            return (
              <StatTile
                key={slug}
                label={role.name}
                value={role.job_count}
                tone="fresh"
                href={`/jobs?category=${slug}`}
              />
            );
          })}
          <StatTile label="All active" value={stats.active_jobs} href="/jobs" />
          <StatTile label="Remote" value={stats.remote_jobs} href="/jobs?remote=REMOTE" />
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

      {/* The feed itself is the members-only part. Anonymous visitors see what the board
          holds and how to ask for access -- never the postings. */}
      {latest ? (
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
      ) : (
        <section className="rounded-xl border border-slate-200 bg-white p-8 text-center dark:border-slate-800 dark:bg-slate-900">
          <h2 className="text-xl font-semibold">Job listings require an approved account</h2>
          <p className="mx-auto mt-2 max-w-lg text-sm text-slate-600 dark:text-slate-400">
            {user
              ? "Your account is awaiting administrator approval. You will get access once it is reviewed."
              : "Request access and an administrator will review your account. Approval unlocks the full board, search and employer directory."}
          </p>
          <div className="mt-6 flex justify-center gap-3">
            {user ? (
              <Link href="/pending" className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-700">
                Check your status
              </Link>
            ) : (
              <>
                <Link href="/register" className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-blue-700">
                  Request access
                </Link>
                <Link href="/login" className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium transition hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800">
                  Sign in
                </Link>
              </>
            )}
          </div>
        </section>
      )}

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
