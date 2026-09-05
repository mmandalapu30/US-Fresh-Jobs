/**
 * Which country's board the visitor is looking at.
 *
 * One set of pages serves both, scoped by a cookie rather than by the URL. The cookie is
 * what makes the switch stick across every page -- browse, search, a job you opened from
 * the India board and came back from -- without threading a query parameter through every
 * link on the site, and without moving the URLs anyone has already bookmarked.
 *
 * The cost is that a link is not self-describing: /jobs means "jobs on whichever board you
 * last chose". Pages that need to be shareable across that line pass ?country= explicitly,
 * which wins over the cookie.
 *
 * This half is deliberately free of `next/headers`: the switcher is a client component and
 * imports these constants, so anything server-only here would break the client bundle at
 * build time. Reading the cookie lives in `country.server.ts`.
 */

export const COUNTRY_COOKIE = "board-country";

export interface Country {
  code: CountryCode;
  label: string;
  /** Shown in the switcher; the board never renders a flag beside a job. */
  flag: string;
  /** What a subdivision is called on this board, for headings and empty states. */
  subdivisionNoun: string;
}

export type CountryCode = "US" | "IN";

export const COUNTRIES: readonly Country[] = [
  { code: "US", label: "United States", flag: "🇺🇸", subdivisionNoun: "state" },
  { code: "IN", label: "India", flag: "🇮🇳", subdivisionNoun: "state" },
];

export const DEFAULT_COUNTRY: CountryCode = "US";

export function isCountryCode(value: string | undefined | null): value is CountryCode {
  return COUNTRIES.some((c) => c.code === value);
}

export function countryOf(code: CountryCode): Country {
  return COUNTRIES.find((c) => c.code === code) ?? COUNTRIES[0];
}
