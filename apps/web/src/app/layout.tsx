import type { Metadata } from "next";
import Link from "next/link";

import { DataFreshness } from "@/components/DataFreshness";

import { AppliedNavLink } from "@/components/AppliedNavLink";
import { AppliedProvider } from "@/components/AppliedProvider";
import { CountrySwitcher } from "@/components/CountrySwitcher";
import { currentCountry } from "@/lib/country.server";

import "./globals.css";

export const metadata: Metadata = {
  title: "US Fresh Jobs",
  description: "A continuously updated U.S. job data platform.",
};

// The freshness strip fetches, so this layout must render per request. Without this the
// web image fails to build: `docker build` has no API to reach (docs/07 section 6).
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const country = await currentCountry();

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
              {/*
                The badge names the board you are on. It used to be a hardcoded "US", which
                was the whole product; with two boards a fixed badge would quietly contradict
                the switch beside it on every India page.
              */}
              <Link href="/" className="flex items-center gap-2 font-semibold">
                <span className="grid h-7 w-7 place-items-center rounded-lg bg-blue-600 text-sm text-white">
                  {country}
                </span>
                <span>Fresh Jobs</span>
              </Link>
              <div className="flex items-center gap-5 text-sm">
                <Link href="/jobs" className="hover:text-blue-600">Browse</Link>
                <Link href="/companies" className="hover:text-blue-600">Companies</Link>
                <Link href="/search" className="hover:text-blue-600">Search</Link>
                <AppliedNavLink />
                <Link href="/admin" className="text-slate-500 hover:text-blue-600">Admin</Link>
                <CountrySwitcher active={country} />
              </div>
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
