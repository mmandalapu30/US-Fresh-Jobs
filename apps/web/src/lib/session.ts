import { cookies } from "next/headers";

/**
 * The browser's session, and what the server knows about it.
 *
 * The token lives in an httpOnly cookie, so page code can read it server-side but no
 * client script ever can -- an XSS bug cannot exfiltrate a session. Nothing here runs in
 * the browser.
 *
 * Deliberately no client-side decoding of the token. The frontend never decides whether
 * someone is approved; it asks the API, which reads the database. Anything this module
 * concludes is for *rendering* -- which page to show, which nav links to draw -- and is
 * re-checked by the backend on every request that carries data.
 */

export const SESSION_COOKIE = "session";

export type UserStatus = "PENDING" | "APPROVED" | "REJECTED" | "SUSPENDED";
export type UserRole = "USER" | "ADMIN" | "SERVICE";

export interface SessionUser {
  id: number;
  name: string | null;
  email: string;
  role: UserRole;
  status: UserStatus;
  created_at?: string | null;
  approved_at?: string | null;
}

export async function getSessionToken(): Promise<string | null> {
  const store = await cookies();
  return store.get(SESSION_COOKIE)?.value ?? null;
}

/**
 * Who the caller is, according to the API.
 *
 * Returns null for "not logged in" *and* for an expired or tampered token, because from
 * the page's point of view those are the same situation: show the public view. The
 * distinction matters to the API, not to the renderer.
 *
 * Uncached on purpose. An approval granted seconds ago must show up on the next
 * navigation, and a cached "PENDING" would leave the user staring at a waiting page after
 * they had already been let in.
 */
export async function getCurrentUser(): Promise<SessionUser | null> {
  const token = await getSessionToken();
  if (!token) return null;

  const base = process.env.API_BASE_URL ?? "http://127.0.0.1:8765/api/v1";
  try {
    const response = await fetch(`${base}/auth/me`, {
      headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as SessionUser;
  } catch {
    // An API that cannot be reached is not an authorization decision. Rendering the public
    // view is the safe failure: it shows less, never more.
    return null;
  }
}

/** Convenience predicates, used only to choose what to render. */
export const isApproved = (u: SessionUser | null): boolean => u?.status === "APPROVED";
export const isAdmin = (u: SessionUser | null): boolean =>
  u?.role === "ADMIN" && u?.status === "APPROVED";
