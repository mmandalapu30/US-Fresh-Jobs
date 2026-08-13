import Link from "next/link";

import { formatNumber } from "@/lib/format";

/**
 * A single headline number.
 *
 * `note` exists so a zero can explain itself. A bare "0 posted in the last hour" reads as
 * a broken feed; "source publishes daily" tells the truth.
 *
 * `href` turns the tile into a drill-down. Where one is given it must resolve to *exactly*
 * the rows the number counted — a tile that says 189 and opens a list of 174 is worse than
 * a tile that does nothing.
 */
export function StatTile({
  label,
  value,
  note,
  tone = "default",
  href,
}: {
  label: string;
  value: number;
  note?: string;
  tone?: "default" | "fresh" | "muted";
  href?: string;
}) {
  const valueTone =
    tone === "fresh"
      ? "text-emerald-600 dark:text-emerald-400"
      : tone === "muted"
        ? "text-slate-500"
        : "text-slate-900 dark:text-slate-100";

  const shell =
    "block rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900";

  const body = (
    <>
      <div className="flex items-center justify-between gap-2">
        <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
        {href ? (
          <span
            aria-hidden
            className="text-slate-300 transition group-hover:translate-x-0.5 group-hover:text-blue-500 dark:text-slate-700"
          >
            →
          </span>
        ) : null}
      </div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${valueTone}`}>
        {formatNumber(value)}
      </div>
      {note ? <div className="mt-1 text-xs text-slate-500">{note}</div> : null}
    </>
  );

  if (!href) return <div className={shell}>{body}</div>;

  return (
    <Link
      href={href}
      className={`${shell} group transition hover:border-blue-400 hover:shadow-sm dark:hover:border-blue-600`}
    >
      {body}
    </Link>
  );
}
