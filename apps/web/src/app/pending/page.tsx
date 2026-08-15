import { redirect } from "next/navigation";

import { logout } from "@/app/(auth)/actions";
import { absoluteTime } from "@/lib/format";
import { getCurrentUser } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata = { title: "Awaiting approval · US Fresh Jobs" };

/** What each non-approved state should say, and how it should feel. */
const STATE = {
  PENDING: {
    tone: "amber",
    heading: "Your account is waiting for administrator approval",
    body: "An administrator will review your request. You will receive access once your account is approved.",
  },
  REJECTED: {
    tone: "red",
    heading: "Your access request was not approved",
    body: "If you believe this was a mistake, contact the administrator who reviews access requests.",
  },
  SUSPENDED: {
    tone: "red",
    heading: "Your account has been suspended",
    body: "Access to job data is paused. Contact an administrator to have it restored.",
  },
} as const;

const TONE = {
  amber: "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200",
  red: "border-red-300 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950 dark:text-red-200",
} as const;

export default async function PendingPage() {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  // An approved user has no business here, and showing them a waiting page after they were
  // let in would be worse than a redirect.
  if (user.status === "APPROVED") redirect("/jobs");

  const state = STATE[user.status as keyof typeof STATE] ?? STATE.PENDING;

  return (
    <div className="mx-auto max-w-lg py-14">
      <div className={`rounded-xl border p-6 ${TONE[state.tone]}`}>
        <h1 className="text-xl font-semibold">{state.heading}</h1>
        <p className="mt-2 text-sm opacity-90">{state.body}</p>
      </div>

      <dl className="mt-6 divide-y divide-slate-200 rounded-xl border border-slate-200 text-sm dark:divide-slate-800 dark:border-slate-800">
        <div className="flex justify-between gap-4 px-4 py-3">
          <dt className="text-slate-500">Account</dt>
          <dd className="font-medium">{user.email}</dd>
        </div>
        <div className="flex justify-between gap-4 px-4 py-3">
          <dt className="text-slate-500">Status</dt>
          <dd className="font-medium">{user.status.toLowerCase()}</dd>
        </div>
        <div className="flex justify-between gap-4 px-4 py-3">
          <dt className="text-slate-500">Requested</dt>
          <dd>{user.created_at ? absoluteTime(user.created_at) : "—"}</dd>
        </div>
      </dl>

      <form action={logout} className="mt-6">
        <button
          type="submit"
          className="rounded-lg border border-slate-300 px-4 py-2 text-sm transition hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-900"
        >
          Sign out
        </button>
      </form>
    </div>
  );
}
