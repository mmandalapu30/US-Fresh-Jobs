import Link from "next/link";

import { UserTable } from "@/components/UserTable";
import { adminApi } from "@/lib/admin";
import { requireAdminPage } from "@/lib/guard";

export const dynamic = "force-dynamic";

export const metadata = { title: "Users · Administration" };

type Search = Promise<Record<string, string | string[] | undefined>>;

const SORTS = [
  { value: "created_desc", label: "Newest first" },
  { value: "created_asc", label: "Oldest first" },
  { value: "email", label: "Email A–Z" },
];

export default async function AllUsersPage({ searchParams }: { searchParams: Search }) {
  await requireAdminPage();
  const params = await searchParams;
  const one = (k: string) => {
    const v = params[k];
    return Array.isArray(v) ? v[0] : v;
  };

  const q = one("q") ?? "";
  const status = one("status") ?? "";
  const role = one("role") ?? "";
  const sort = one("sort") ?? "created_desc";
  const page = Math.max(1, Number(one("page") ?? 1) || 1);

  const data = await adminApi.users({ q, status, role, sort, page, page_size: 25 });
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));

  // Preserve the filters when paging; losing them on "next" is the classic annoyance here.
  const href = (patch: Record<string, string | number>) => {
    const next = new URLSearchParams();
    for (const [k, v] of Object.entries({ q, status, role, sort, page, ...patch })) {
      if (v !== "" && v !== undefined && v !== null) next.set(k, String(v));
    }
    return `/admin/users?${next.toString()}`;
  };

  return (
    <div className="space-y-4">
      <form className="flex flex-wrap items-end gap-2" action="/admin/users">
        <div className="min-w-48 flex-1">
          <label htmlFor="q" className="block text-xs font-medium text-slate-500">
            Search
          </label>
          <input
            id="q"
            name="q"
            defaultValue={q}
            placeholder="name or email"
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
          />
        </div>
        <div>
          <label htmlFor="status" className="block text-xs font-medium text-slate-500">
            Status
          </label>
          <select
            id="status"
            name="status"
            defaultValue={status}
            className="mt-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
          >
            <option value="">Any</option>
            {["PENDING", "APPROVED", "REJECTED", "SUSPENDED"].map((s) => (
              <option key={s} value={s}>
                {s.charAt(0) + s.slice(1).toLowerCase()}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="role" className="block text-xs font-medium text-slate-500">
            Role
          </label>
          <select
            id="role"
            name="role"
            defaultValue={role}
            className="mt-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
          >
            <option value="">Any</option>
            <option value="USER">User</option>
            <option value="ADMIN">Admin</option>
          </select>
        </div>
        <div>
          <label htmlFor="sort" className="block text-xs font-medium text-slate-500">
            Sort
          </label>
          <select
            id="sort"
            name="sort"
            defaultValue={sort}
            className="mt-1 rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
          >
            {SORTS.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </select>
        </div>
        <button
          type="submit"
          className="rounded-lg bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-blue-700"
        >
          Apply
        </button>
      </form>

      <p className="text-sm text-slate-500">
        {data.total.toLocaleString()} user{data.total === 1 ? "" : "s"}
        {data.total > data.page_size ? ` · page ${data.page} of ${pages}` : ""}
      </p>

      <UserTable users={data.items} empty="No users match these filters." />

      {pages > 1 ? (
        <div className="flex items-center justify-between text-sm">
          {page > 1 ? (
            <Link href={href({ page: page - 1 })} className="text-blue-600 hover:underline">
              ← Previous
            </Link>
          ) : (
            <span />
          )}
          {page < pages ? (
            <Link href={href({ page: page + 1 })} className="text-blue-600 hover:underline">
              Next →
            </Link>
          ) : (
            <span />
          )}
        </div>
      ) : null}
    </div>
  );
}
