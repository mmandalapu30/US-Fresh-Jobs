import { requireApprovedPage } from "@/lib/guard";

export const dynamic = "force-dynamic";

/**
 * Gate for the applied list.
 *
 * The page itself is a client component reading browser-local state, so it cannot run a
 * server-side check. A layout can, and Next renders it first -- which keeps the guard on
 * the server where it belongs rather than in code the visitor controls.
 *
 * Nothing here is job data from the API; it is the visitor's own snapshots. The gate is
 * about the product being members-only, not about data protection, and the distinction is
 * worth keeping straight.
 */
export default async function AppliedLayout({ children }: { children: React.ReactNode }) {
  await requireApprovedPage();
  return <>{children}</>;
}
