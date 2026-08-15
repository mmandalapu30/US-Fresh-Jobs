import Link from "next/link";

export const metadata = { title: "Request submitted · US Fresh Jobs" };

export default function SubmittedPage() {
  return (
    <div className="mx-auto max-w-lg py-16 text-center">
      <div className="mx-auto mb-5 grid h-12 w-12 place-items-center rounded-full bg-emerald-100 text-2xl dark:bg-emerald-950">
        ✓
      </div>
      <h1 className="text-2xl font-bold">Your account has been submitted for approval</h1>
      <p className="mt-3 text-slate-600 dark:text-slate-400">
        An administrator will review your request. You will receive access once your account
        is approved.
      </p>
      <Link
        href="/login"
        className="mt-8 inline-block rounded-lg bg-blue-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-blue-700"
      >
        Go to sign in
      </Link>
    </div>
  );
}
