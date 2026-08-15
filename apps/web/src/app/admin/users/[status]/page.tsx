import { notFound } from "next/navigation";

import { UserTable } from "@/components/UserTable";
import { adminApi } from "@/lib/admin";
import { requireAdminPage } from "@/lib/guard";

export const dynamic = "force-dynamic";

/**
 * The status-scoped views: /admin/users/pending and friends.
 *
 * One route rather than four near-identical pages -- the only thing that varies is the
 * filter and the wording when the list is empty.
 */
const VIEWS: Record<string, { title: string; empty: string }> = {
  pending: {
    title: "Pending approval",
    empty: "Nobody is waiting for approval.",
  },
  approved: { title: "Approved users", empty: "No approved users yet." },
  rejected: { title: "Rejected requests", empty: "No requests have been rejected." },
  suspended: { title: "Suspended accounts", empty: "No accounts are suspended." },
};

export async function generateMetadata({
  params,
}: {
  params: Promise<{ status: string }>;
}) {
  const { status } = await params;
  return { title: `${VIEWS[status]?.title ?? "Users"} · Administration` };
}

export default async function StatusUsersPage({
  params,
}: {
  params: Promise<{ status: string }>;
}) {
  await requireAdminPage();
  const { status } = await params;
  const view = VIEWS[status];
  if (!view) notFound();

  const data = await adminApi.users({
    status: status.toUpperCase(),
    // Oldest first: whoever has been waiting longest should be dealt with first.
    sort: status === "pending" ? "created_asc" : "created_desc",
    page_size: 100,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold">{view.title}</h2>
        <span className="text-sm text-slate-500">
          {data.total.toLocaleString()} user{data.total === 1 ? "" : "s"}
        </span>
      </div>
      <UserTable users={data.items} empty={view.empty} />
    </div>
  );
}
