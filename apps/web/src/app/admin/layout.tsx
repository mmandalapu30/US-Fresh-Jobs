import Link from "next/link";
import { redirect } from "next/navigation";

import { adminApi } from "@/lib/admin";
import { getCurrentUser, isAdmin } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * The console's shell, and its first gate.
 *
 * This redirect is a courtesy, not a control. Every /admin API call is independently
 * authorized by the backend against the database, so removing this layout would change
 * what a non-admin *sees*, never what they can *read*.
 */
export default async function AdminLayout({ children }: { children: React.ReactNode }) {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (!isAdmin(user)) redirect(user.status === "APPROVED" ? "/jobs" : "/pending");

  // A count beside the link, so "someone is waiting" is visible without navigating.
  let pending = 0;
  try {
    pending = (await adminApi.summary()).pending;
  } catch {
    /* the page below will surface the failure; the badge simply stays absent */
  }

  const tabs = [
    { href: "/admin", label: "Overview" },
    { href: "/admin/users/pending", label: "Pending", badge: pending },
    { href: "/admin/users/approved", label: "Approved" },
    { href: "/admin/users/rejected", label: "Rejected" },
    { href: "/admin/users/suspended", label: "Suspended" },
    { href: "/admin/users", label: "All users" },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-bold">Administration</h1>
        <p className="text-sm text-slate-500">Signed in as {user.email}</p>
      </div>

      <nav className="flex flex-wrap gap-1 border-b border-slate-200 dark:border-slate-800">
        {tabs.map((t) => (
          <Link
            key={t.href}
            href={t.href}
            className="relative rounded-t-lg px-3 py-2 text-sm text-slate-600 transition hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-900 dark:hover:text-slate-100"
          >
            {t.label}
            {t.badge ? (
              <span className="ml-1.5 rounded-full bg-amber-500 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                {t.badge}
              </span>
            ) : null}
          </Link>
        ))}
      </nav>

      {children}
    </div>
  );
}
