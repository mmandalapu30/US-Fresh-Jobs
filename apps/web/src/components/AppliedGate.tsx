"use client";

import { useApplied } from "./AppliedProvider";

/**
 * Removes a job from the feed once this browser has marked it applied.
 *
 * Only the *decision* is client-side — the children stay a server-rendered subtree. That
 * keeps the card's relative timestamps ("Found by us 3 hours ago") on the server, where
 * they cannot disagree with the browser's clock at hydration.
 */
export function AppliedGate({
  jobId,
  enabled = true,
  children,
}: {
  jobId: number;
  /** False on the applied list itself, where hiding applied jobs would empty the page. */
  enabled?: boolean;
  children: React.ReactNode;
}) {
  const { isApplied } = useApplied();
  if (enabled && isApplied(jobId)) return null;
  return <>{children}</>;
}
