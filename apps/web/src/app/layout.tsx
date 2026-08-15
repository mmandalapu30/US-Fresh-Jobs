import type { Metadata } from "next";
import Link from "next/link";

import { DataFreshness } from "@/components/DataFreshness";
import { SessionNav } from "@/components/SessionNav";

import { AppliedNavLink } from "@/components/AppliedNavLink";
import { AppliedProvider } from "@/components/AppliedProvider";

import "./globals.css";

export const metadata: Metadata = {
  title: "US Fresh Jobs",
  description: "A continuously updated U.S. job data platform.",
};

// The freshness strip fetches, so this layout must render per request. Without this the
// web image fails to build: `docker build` has no API to reach (docs/07 section 6).
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        {/*
          The applied list is browser-local state, so it is provided above the whole tree:
          the header badge, the feed cards and the applied page all read one source and
          stay in step without any of them refetching.
        */}
        <AppliedProvider>
          <header className="sticky top-0 z-40 border-b border-slate-200 bg-white/85 backdrop-blur dark:border-slate-800 dark:bg-slate-950/85">
            <nav className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
              <Link href="/" className="flex items-center gap-2 font-semibold">
                <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-600 text-sm text-white">
                  US
                </span>
                <span>Fresh Jobs</span>
              </Link>
              {/* Which links exist at all depends on who is signed in. Hiding a link is
                  not protection -- every destination is guarded server-side -- but
                  offering a visitor a page that will only redirect them is poor manners. */}
              <SessionNav />
            </nav>
          </header>

          <DataFreshness />

          <main className="mx-auto max-w-6xl px-4 py-8">{children}</main>

          <footer className="mx-auto max-w-6xl px-4 py-10 text-xs text-slate-500">
            Job data is provided by third-party sources under their respective licences.
            Posting dates come from the source; detection times are ours. Neither is ever
            fabricated.
          </footer>
        </AppliedProvider>
      </body>
    </html>
  );
}
