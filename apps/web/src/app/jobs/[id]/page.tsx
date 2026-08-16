import Link from "next/link";
import { notFound } from "next/navigation";

import { AppliedButton } from "@/components/AppliedButton";
import { ApiError, api } from "@/lib/api";
import {
  EMPLOYMENT_LABEL,
  REMOTE_LABEL,
  absoluteTime,
  formatSalary,
  locationLabel,
  postedLabel,
  relativeTime,
} from "@/lib/format";

export const revalidate = 60;

export default async function JobDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;

  let job;
  try {
    job = await api.job(id);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  const salary = formatSalary(job);
  const posted = postedLabel(job);
  const isClosed = job.status !== "ACTIVE";

  return (
    <article className="mx-auto max-w-3xl space-y-6">
      <Link href="/jobs" className="text-sm text-blue-600 hover:underline">
        ← Back to jobs
      </Link>

      <header className="space-y-3">
        {isClosed ? (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200">
            This job is no longer active. It is kept for history rather than deleted.
          </div>
        ) : null}

        <h1 className="text-3xl font-bold tracking-tight">{job.title}</h1>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-slate-600 dark:text-slate-400">
          <span className="font-medium text-slate-800 dark:text-slate-200">
            {job.company_name ?? "Company not stated"}
          </span>
          <span aria-hidden>·</span>
          <span>{locationLabel(job)}</span>
          <span aria-hidden>·</span>
          <span>{REMOTE_LABEL[job.remote_type]}</span>
          {job.employment_type !== "UNKNOWN" ? (
            <>
              <span aria-hidden>·</span>
              <span>{EMPLOYMENT_LABEL[job.employment_type] ?? job.employment_type}</span>
            </>
          ) : null}
        </div>

        {salary ? (
          <p className="text-xl font-semibold text-emerald-700 dark:text-emerald-400">{salary}</p>
        ) : (
          <p className="text-sm text-slate-500">Salary not published by the employer</p>
        )}

        <div className="flex flex-wrap items-center gap-3">
          {job.apply_url ? (
            <a
              href={job.apply_url}
              target="_blank"
              rel="noopener noreferrer nofollow"
              className="inline-block rounded-lg bg-blue-600 px-5 py-2.5 font-medium text-white transition hover:bg-blue-700"
            >
              Apply for this job ↗
            </a>
          ) : null}
          {/* Applying happens on the employer's site, so marking it is a separate act. */}
          <AppliedButton job={job} size="lg" />
        </div>
      </header>

      {/*
        The spec is explicit that these two must be distinguishable. They are separate
        facts from separate clocks: one is what the employer stated, the other is when our
        ingestion first saw the posting. Neither is ever substituted for the other.
      */}
      <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Timing
        </h2>
        <dl className="grid gap-4 sm:grid-cols-2">
          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Posted by employer
            </dt>
            <dd className="mt-1">
              {posted.trusted ? (
                <>
                  <div className="font-medium">{absoluteTime(job.posted_at)}</div>
                  <div className="text-sm text-slate-500">{relativeTime(job.posted_at)}</div>
                </>
              ) : (
                <div className="text-sm italic text-slate-500">{posted.text}</div>
              )}
            </dd>
          </div>

          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Detected by our platform
            </dt>
            <dd className="mt-1">
              <div className="font-medium">{absoluteTime(job.first_seen_at)}</div>
              <div className="text-sm text-slate-500">{relativeTime(job.first_seen_at)}</div>
            </dd>
          </div>

          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              Last confirmed present
            </dt>
            <dd className="mt-1 text-sm">{absoluteTime(job.last_seen_at)}</dd>
          </div>

          <div>
            <dt className="text-xs font-medium uppercase tracking-wide text-slate-500">
              {job.close_at ? "Employer close date" : "Detected closed"}
            </dt>
            <dd className="mt-1 text-sm">
              {job.close_at ? absoluteTime(job.close_at) : job.closed_at ? absoluteTime(job.closed_at) : "—"}
            </dd>
          </div>
        </dl>
      </section>

      {job.description_text ? (
        <section className="rounded-xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
            Description
          </h2>
          {/*
            Rendered as plain text on purpose. The source's description_html is
            third-party markup containing inline base64 images and arbitrary tags; until
            the sanitisation pass lands it is not safe to inject, and showing text is
            better than showing nothing or showing a security hole.
          */}
          <div className="whitespace-pre-wrap text-sm leading-relaxed text-slate-700 dark:text-slate-300">
            {job.description_text}
          </div>
        </section>
      ) : (
        <p className="text-sm text-slate-500">No description provided by the source.</p>
      )}

    </article>
  );
}
