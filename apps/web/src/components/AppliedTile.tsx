"use client";

import { useApplied } from "./AppliedProvider";
import { StatTile } from "./StatTile";

/**
 * The applied count, alongside the platform stats.
 *
 * It reads zero on the server and on the first client render, then fills in — that is the
 * hydration-safe ordering described in AppliedProvider, not a loading bug.
 */
export function AppliedTile() {
  const { entries, ready } = useApplied();
  const count = Object.keys(entries).length;

  return (
    <StatTile
      label="Applied"
      value={count}
      href="/applied"
      tone={count > 0 ? "fresh" : "muted"}
      note={ready && count === 0 ? "mark jobs as you apply" : "saved in this browser"}
    />
  );
}
