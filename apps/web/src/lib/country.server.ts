/**
 * Server half of the country switch: reading the active board out of the request.
 *
 * Separate from `country.ts` because that module is imported by the switcher, which is a
 * client component -- and `next/headers` cannot be in a client bundle. Keeping the split
 * explicit beats discovering it as a webpack error on the next build.
 */

import { cookies } from "next/headers";

import { COUNTRY_COOKIE, DEFAULT_COUNTRY, isCountryCode, type CountryCode } from "@/lib/country";

/**
 * The active board, from an explicit ?country= if present, otherwise the cookie.
 *
 * An unrecognised value falls back to the default rather than being passed through: the
 * API would answer a two-letter code we do not ingest with an empty board, which reads as
 * "there are no jobs" rather than "that is not one of the boards".
 */
export async function currentCountry(explicit?: string): Promise<CountryCode> {
  if (isCountryCode(explicit)) return explicit;
  const cookie = (await cookies()).get(COUNTRY_COOKIE)?.value;
  return isCountryCode(cookie) ? cookie : DEFAULT_COUNTRY;
}
