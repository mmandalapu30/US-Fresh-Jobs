"use server";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { SESSION_COOKIE } from "@/lib/session";

/**
 * Server actions for the auth forms.
 *
 * These run on the server, so the API token never reaches the browser as a value it can
 * read -- it goes straight into an httpOnly cookie. The browser only ever sees the
 * redirect that follows.
 *
 * None of these decide anything about access. They exchange credentials for a session and
 * send the visitor somewhere; whether that session can see jobs is settled by the API on
 * every subsequent request.
 */

const API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8765/api/v1";

export interface FormState {
  error?: string;
  /** Field-level messages, so the form can mark the offending input rather than shouting. */
  fieldErrors?: Record<string, string>;
}

/** Turn the API's error envelope into something a person should read. */
async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const body = (await response.json()) as {
      error?: { message?: string };
      detail?: { msg?: string }[] | string;
    };
    if (body.error?.message) return body.error.message;
    // FastAPI's validation errors arrive as a list; the first one is the useful one.
    if (Array.isArray(body.detail) && body.detail[0]?.msg) {
      return String(body.detail[0].msg).replace(/^Value error,\s*/, "");
    }
    if (typeof body.detail === "string") return body.detail;
  } catch {
    /* fall through to the generic message */
  }
  return fallback;
}

export async function login(_prev: FormState, formData: FormData): Promise<FormState> {
  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");
  if (!email || !password) return { error: "Enter your email and password." };

  let response: Response;
  try {
    response = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
      cache: "no-store",
    });
  } catch {
    return { error: "Could not reach the server. Try again in a moment." };
  }

  if (!response.ok) {
    return { error: await readError(response, "Email or password is incorrect.") };
  }

  const body = (await response.json()) as {
    access_token: string;
    user: { status: string; role: string };
  };

  const store = await cookies();
  store.set(SESSION_COOKIE, body.access_token, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 15,
  });

  // Land people where their status means something. Sending a pending user to /jobs would
  // show them a refusal they did not ask for; /pending explains it.
  if (body.user.status !== "APPROVED") redirect("/pending");
  redirect(body.user.role === "ADMIN" ? "/admin" : "/jobs");
}

export async function register(_prev: FormState, formData: FormData): Promise<FormState> {
  const payload = {
    name: String(formData.get("name") ?? "").trim(),
    email: String(formData.get("email") ?? "").trim(),
    password: String(formData.get("password") ?? ""),
    phone: String(formData.get("phone") ?? "").trim() || null,
  };

  const fieldErrors: Record<string, string> = {};
  if (!payload.name) fieldErrors.name = "Tell us your name.";
  if (!payload.email.includes("@")) fieldErrors.email = "Enter a valid email address.";
  // Mirrors the server's rule so the common mistake is caught without a round trip. The
  // server enforces it regardless -- this is convenience, not validation.
  if (payload.password.length < 12) {
    fieldErrors.password = "Use at least 12 characters.";
  }
  if (Object.keys(fieldErrors).length > 0) return { fieldErrors };

  let response: Response;
  try {
    response = await fetch(`${API_BASE}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });
  } catch {
    return { error: "Could not reach the server. Try again in a moment." };
  }

  if (!response.ok) {
    return { error: await readError(response, "Could not create the account.") };
  }

  redirect("/register/submitted");
}

export async function logout(): Promise<void> {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
  redirect("/");
}
