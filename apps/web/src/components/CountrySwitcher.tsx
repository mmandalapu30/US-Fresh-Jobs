"use client";

import { useRouter } from "next/navigation";
import { useTransition } from "react";

import { COUNTRIES, COUNTRY_COOKIE, type CountryCode } from "@/lib/country";

/**
 * Switches the board between countries.
 *
 * Writes the cookie from the client and calls router.refresh(), rather than posting to a
 * server action: every page is already `force-dynamic`, so a refresh re-runs them against
 * the new cookie and no route needs to know the switcher exists. useTransition keeps the
 * current board on screen while that happens instead of flashing a skeleton.
 *
 * The cookie is deliberately not HttpOnly -- this component is the only thing that writes
 * it, it carries no authority, and the alternative is a round trip to set a preference.
 */
export function CountrySwitcher({ active }: { active: CountryCode }) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  const select = (code: CountryCode) => {
    if (code === active) return;
    // A year, so the choice survives the gap between one job hunt and the next.
    document.cookie = `${COUNTRY_COOKIE}=${code}; path=/; max-age=31536000; samesite=lax`;
    startTransition(() => router.refresh());
  };

  return (
    <div
      role="group"
      aria-label="Job board country"
      data-pending={pending ? "" : undefined}
      className="flex items-center gap-0.5 rounded-lg border border-slate-200 bg-white p-0.5 text-xs dark:border-slate-800 dark:bg-slate-900"
    >
      {COUNTRIES.map((country) => {
        const selected = country.code === active;
        return (
          <button
            key={country.code}
            type="button"
            onClick={() => select(country.code)}
            aria-pressed={selected}
            disabled={pending}
            className={
              selected
                ? "rounded-md bg-blue-600 px-2 py-1 font-medium text-white"
                : "rounded-md px-2 py-1 text-slate-600 hover:bg-slate-100 disabled:opacity-60 dark:text-slate-300 dark:hover:bg-slate-800"
            }
          >
            <span aria-hidden className="mr-1">
              {country.flag}
            </span>
            {country.code}
            <span className="sr-only"> — {country.label}</span>
          </button>
        );
      })}
    </div>
  );
}
