export const metadata = { title: "About · US Fresh Jobs" };

export default function AboutPage() {
  return (
    <div className="mx-auto max-w-2xl py-10">
      <h1 className="text-3xl font-bold tracking-tight">About</h1>
      <div className="mt-6 space-y-4 text-slate-600 dark:text-slate-400">
        <p>
          A continuously updated U.S. job data platform. The database preserves every
          qualifying job it has ever seen; the board surfaces the newest and most relevant.
        </p>
        <p>
          Every posting keeps two dates that are never conflated: when the employer says it
          was posted, and when this platform first detected it. Where the employer&apos;s date
          is missing or implausible, we say so rather than guessing.
        </p>
        <p>
          Access to job data is granted by an administrator. Request access and you will be
          notified once your account is reviewed.
        </p>
      </div>
    </div>
  );
}
