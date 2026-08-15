import { getSessionToken } from "./session";

/**
 * The admin console's data access.
 *
 * Separate from `lib/api.ts` because these calls are never cached and never shared: every
 * one is a privileged read of user records, and a cache keyed only on URL would serve one
 * administrator's view to whoever asked next.
 */

const API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8765/api/v1";

export type UserStatus = "PENDING" | "APPROVED" | "REJECTED" | "SUSPENDED";

export interface AdminUser {
  id: number;
  name: string | null;
  email: string;
  phone: string | null;
  role: string;
  status: UserStatus;
  created_at: string;
  approved_at: string | null;
  rejected_at: string | null;
  suspended_at: string | null;
  last_login_at: string | null;
}

export interface UserPage {
  items: AdminUser[];
  total: number;
  page: number;
  page_size: number;
}

export interface AdminSummary {
  total_users: number;
  pending: number;
  approved: number;
  rejected: number;
  suspended: number;
  admins: number;
}

export interface AuditEntry {
  id: number;
  action: string;
  admin_email: string | null;
  target_email: string | null;
  previous_status: string | null;
  new_status: string | null;
  created_at: string;
}

export class AdminApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function adminGet<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  const url = new URL(`${API_BASE}${path}`);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    url.searchParams.set(k, String(v));
  }
  const token = await getSessionToken();
  const response = await fetch(url, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  if (!response.ok) {
    throw new AdminApiError(`${path} -> ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

export const adminApi = {
  summary: () => adminGet<AdminSummary>("/admin/summary"),
  users: (params: Record<string, unknown> = {}) => adminGet<UserPage>("/admin/users", params),
  user: (id: number | string) => adminGet<AdminUser>(`/admin/users/${id}`),
  audit: (limit = 15) => adminGet<AuditEntry[]>("/admin/audit", { limit }),
};
