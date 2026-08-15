import { redirect } from "next/navigation";

import { getCurrentUser, type SessionUser } from "./session";

/**
 * The page-level gate for anything showing job data.
 *
 * Returns the user, or redirects. It never renders a refusal, because there is a better
 * page for every case: /login explains how to get in, /pending explains why you are not.
 *
 * This is presentation. The API refuses the same request independently, so a page that
 * forgot to call this would render an error rather than leak data -- which is the property
 * that matters, and the reason the backend was built first.
 */
export async function requireApprovedPage(): Promise<SessionUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (user.status !== "APPROVED") redirect("/pending");
  return user;
}

/**
 * The same gate for administrative pages.
 *
 * Needed on the page as well as the layout: Next renders them in parallel, so a page that
 * fetches privileged data would do so before the layout's redirect took effect and throw
 * instead of redirecting.
 */
export async function requireAdminPage(): Promise<SessionUser> {
  const user = await getCurrentUser();
  if (!user) redirect("/login");
  if (user.role !== "ADMIN" || user.status !== "APPROVED") {
    redirect(user.status === "APPROVED" ? "/jobs" : "/pending");
  }
  return user;
}
