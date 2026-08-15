import Link from "next/link";
import { redirect } from "next/navigation";

import { RegisterForm } from "@/components/RegisterForm";
import { getCurrentUser } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata = { title: "Request access · US Fresh Jobs" };

export default async function RegisterPage() {
  const user = await getCurrentUser();
  if (user) redirect(user.status === "APPROVED" ? "/jobs" : "/pending");

  return (
    <div className="mx-auto max-w-sm py-10">
      <h1 className="text-2xl font-bold">Request access</h1>
      <p className="mt-1 mb-6 text-sm text-slate-600 dark:text-slate-400">
        Accounts are reviewed by an administrator before job data becomes available.
      </p>

      <RegisterForm />

      <p className="mt-6 text-sm text-slate-600 dark:text-slate-400">
        Already have an account?{" "}
        <Link href="/login" className="text-blue-600 hover:underline">
          Sign in
        </Link>
      </p>
    </div>
  );
}
