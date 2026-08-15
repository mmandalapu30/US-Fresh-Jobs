import { StatusBadge } from "@/components/StatusBadge";
import { UserActions } from "@/components/UserActions";
import type { AdminUser } from "@/lib/admin";
import { absoluteTime, relativeTime } from "@/lib/format";

/**
 * The user list.
 *
 * A table on wide screens and stacked cards on narrow ones -- a horizontally scrolling
 * table is technically responsive and practically unusable on a phone, which is where an
 * administrator is most likely to be approving someone in a spare minute.
 */
export function UserTable({ users, empty }: { users: AdminUser[]; empty: string }) {
  if (users.length === 0) {
    return (
      <p className="rounded-xl border border-dashed border-slate-300 p-10 text-center text-sm text-slate-500 dark:border-slate-700">
        {empty}
      </p>
    );
  }

  return (
    <>
      {/* Narrow screens */}
      <ul className="space-y-3 md:hidden">
        {users.map((u) => (
          <li
            key={u.id}
            className="rounded-xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate font-medium">{u.name ?? "—"}</div>
                <div className="truncate text-sm text-slate-500">{u.email}</div>
              </div>
              <StatusBadge status={u.status} />
            </div>
            <div className="mt-2 text-xs text-slate-500">
              Requested {relativeTime(u.created_at)}
              {u.role === "ADMIN" ? " · administrator" : ""}
            </div>
            <div className="mt-3">
              <UserActions user={u} />
            </div>
          </li>
        ))}
      </ul>

      {/* Wide screens */}
      <div className="hidden overflow-x-auto rounded-xl border border-slate-200 md:block dark:border-slate-800">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500 dark:bg-slate-900">
            <tr>
              <th className="px-4 py-2.5 font-medium">Name</th>
              <th className="px-4 py-2.5 font-medium">Email</th>
              <th className="px-4 py-2.5 font-medium">Registered</th>
              <th className="px-4 py-2.5 font-medium">Status</th>
              <th className="px-4 py-2.5 font-medium">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-200 dark:divide-slate-800">
            {users.map((u) => (
              <tr key={u.id} className="align-middle">
                <td className="px-4 py-3">
                  {u.name ?? "—"}
                  {u.role === "ADMIN" ? (
                    <span className="ml-2 rounded bg-blue-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                      admin
                    </span>
                  ) : null}
                </td>
                <td className="px-4 py-3 text-slate-600 dark:text-slate-400">{u.email}</td>
                <td className="px-4 py-3 whitespace-nowrap" title={absoluteTime(u.created_at)}>
                  {relativeTime(u.created_at)}
                </td>
                <td className="px-4 py-3">
                  <StatusBadge status={u.status} />
                </td>
                <td className="px-4 py-3">
                  <UserActions user={u} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
