import Link from "next/link";

import { logout } from "@/app/(auth)/actions";
import { AppliedNavLink } from "@/components/AppliedNavLink";
import { adminApi } from "@/lib/admin";
import { getCurrentUser, isAdmin, isApproved } from "@/lib/session";

/**
 * Navigation that reflects who is signed in.
 *
 * Purely presentational: every destination enforces its own access server-side, so this
 * decides what to *offer*, never what is permitted. Showing an approved-only link to an
 * anonymous visitor would not leak anything -- it would just send them to a redirect.
 */
export async function SessionNav() {
  const user = await getCurrentUser();
  const approved = isApproved(user);
  const admin = isAdmin(user);

  // The count is the whole point of the badge, so a failure to read it hides the badge
  // rather than breaking the header.
  let pending = 0;
  if (admin) {
    try {
      pending = (await adminApi.summary()).pending;
    } catch {
      /* leave the badge off */
    }
  }

  return (
    <div className="flex items-center gap-4 text-sm">
      {approved ? (
        <>
          <Link href="/jobs" className="hover:text-blue-600">
            Browse
          </Link>
          <Link href="/companies" className="hover:text-blue-600">
            Companies
          </Link>
          <Link href="/search" className="hover:text-blue-600">
            Search
          </Link>
          <AppliedNavLink />
        </>
      ) : (
        <Link href="/about" className="hover:text-blue-600">
          About
        </Link>
      )}

      {admin ? (
        <Link href="/admin" className="relative text-slate-500 hover:text-blue-600">
          Admin
          {pending > 0 ? (
            <span className="absolute -right-3 -top-2 rounded-full bg-amber-500 px-1.5 text-[10px] font-semibold text-white">
              {pending}
            </span>
          ) : null}
        </Link>
      ) : null}

      {user ? (
        <form action={logout}>
          <button
            type="submit"
            className="text-slate-500 transition hover:text-blue-600"
            title={user.email}
          >
            Sign out
          </button>
        </form>
      ) : (
        <>
          <Link href="/login" className="hover:text-blue-600">
            Sign in
          </Link>
          <Link
            href="/register"
            className="rounded-lg bg-blue-600 px-3 py-1.5 font-medium text-white transition hover:bg-blue-700"
          >
            Request access
          </Link>
        </>
      )}
    </div>
  );
}
