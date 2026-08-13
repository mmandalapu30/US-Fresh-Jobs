"use client";

import Link from "next/link";

import { useApplied } from "./AppliedProvider";

/** Header link to the applied list, with a count once there is one to show. */
export function AppliedNavLink() {
  const { entries } = useApplied();
  const count = Object.keys(entries).length;

  return (
    <Link href="/applied" className="flex items-center gap-1.5 hover:text-blue-600">
      Applied
      {count > 0 ? (
        <span className="rounded-full bg-emerald-600 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-white">
          {count}
        </span>
      ) : null}
    </Link>
  );
}
