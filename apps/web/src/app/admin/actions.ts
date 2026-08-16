"use server";

/**
 * Server actions for the "load new jobs" button.
 *
 * These run on the server, so the browser never talks to the API directly — which is what
 * keeps the API unreachable from the internet, the same rule every other page follows.
 */

const API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8765/api/v1";

export interface LoadState {
  id?: number | null;
  status: string;
  message?: string | null;
  busy: boolean;
  retry_after: number;
}

const UNREACHABLE: LoadState = {
  status: "FAILED",
  message: "Could not reach the server.",
  busy: false,
  retry_after: 0,
};

export async function loadNewJobs(): Promise<LoadState> {
  try {
    const response = await fetch(`${API_BASE}/admin/ingest`, {
      method: "POST",
      cache: "no-store",
    });
    // 202 and 200 both carry a state to render; only a transport failure is an error here.
    return (await response.json()) as LoadState;
  } catch {
    return UNREACHABLE;
  }
}

export async function getLoadStatus(): Promise<LoadState> {
  try {
    const response = await fetch(`${API_BASE}/admin/ingest`, { cache: "no-store" });
    if (!response.ok) return { status: "IDLE", busy: false, retry_after: 0 };
    return (await response.json()) as LoadState;
  } catch {
    return { status: "IDLE", busy: false, retry_after: 0 };
  }
}
