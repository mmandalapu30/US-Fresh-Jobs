import Link from "next/link";
import { redirect } from "next/navigation";

import { LoginForm } from "@/components/LoginForm";
import { getCurrentUser } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata = { title: "Sign in · US Fresh Jobs" };

export default async function LoginPage() {
  // Already signed in? Send them where their status means something rather than showing a
  // form they do not need.
  const user = await getCurrentUser();
  if (user) redirect(user.status === "APPROVED" ? "/jobs" : "/pending");

  return (
    <div className="mx-auto max-w-sm py-10">
      <h1 className="text-2xl font-bold">Sign in</h1>
      <p className="mt-1 mb-6 text-sm text-slate-600 dark:text-slate-400">
        Access to job data requires an approved account.
      </p>

      <LoginForm />

      <p className="mt-6 text-sm text-slate-600 dark:text-slate-400">
        No account yet?{" "}
        <Link href="/register" className="text-blue-600 hover:underline">
          Request access
        </Link>
      </p>
    </div>
  );
}
