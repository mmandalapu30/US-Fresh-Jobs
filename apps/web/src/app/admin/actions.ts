"use server";

import { revalidatePath } from "next/cache";

import { getSessionToken } from "@/lib/session";

/**
 * Administrative decisions.
 *
 * These forward the session and nothing else -- no role, no claim about who is asking.
 * The API decides whether the caller is an administrator by reading the database, so a
 * forged form post from a normal user's browser is refused there, not here.
 */

const API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8765/api/v1";

export type AdminAction = "approve" | "reject" | "suspend" | "reactivate";

export interface ActionResult {
  ok: boolean;
  message: string;
}

export async function decideOnUser(
  userId: number,
  action: AdminAction,
): Promise<ActionResult> {
  const token = await getSessionToken();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/admin/users/${userId}/${action}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      cache: "no-store",
    });
  } catch {
    return { ok: false, message: "Could not reach the server." };
  }

  if (!response.ok) {
    let message = `Could not ${action} this user.`;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      if (body.error?.message) message = body.error.message;
    } catch {
      /* keep the generic message */
    }
    return { ok: false, message };
  }

  // Every admin surface reads the same rows, so a decision has to refresh all of them --
  // otherwise the counts on the dashboard disagree with the table the admin just acted on.
  revalidatePath("/admin");
  revalidatePath("/admin/users");

  const past: Record<AdminAction, string> = {
    approve: "approved",
    reject: "rejected",
    suspend: "suspended",
    reactivate: "reactivated",
  };
  return { ok: true, message: `User ${past[action]}.` };
}


export interface FetchState {
  id?: number | null;
  status: string;
  message?: string | null;
  busy: boolean;
}

/**
 * Ask for an on-demand fetch.
 *
 * 202, not 200: the request has been accepted, not carried out. A timer on the host
 * claims it within the minute and runs the ingest with the same image and settings the
 * daily schedule uses.
 */
export async function requestFetch(): Promise<FetchState> {
  const token = await getSessionToken();
  try {
    const response = await fetch(`${API_BASE}/admin/ingest`, {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: "no-store",
    });
    if (!response.ok) {
      return { status: "FAILED", message: "Could not queue a fetch.", busy: false };
    }
    return (await response.json()) as FetchState;
  } catch {
    return { status: "FAILED", message: "Could not reach the server.", busy: false };
  }
}

export async function getFetchStatus(): Promise<FetchState> {
  const token = await getSessionToken();
  try {
    const response = await fetch(`${API_BASE}/admin/ingest`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      cache: "no-store",
    });
    if (!response.ok) {
      return { status: "IDLE", busy: false };
    }
    return (await response.json()) as FetchState;
  } catch {
    return { status: "IDLE", busy: false };
  }
}
