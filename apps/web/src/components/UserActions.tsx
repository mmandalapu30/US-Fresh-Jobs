"use client";

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { decideOnUser, type AdminAction } from "@/app/admin/actions";
import type { AdminUser } from "@/lib/admin";

/**
 * The approve / reject / suspend controls.
 *
 * Every action confirms first. These are not undoable from the console -- a rejection is
 * visible to the person it affects -- so a misplaced click should cost a sentence, not an
 * apology.
 */

const LABEL: Record<AdminAction, string> = {
  approve: "Approve",
  reject: "Reject",
  suspend: "Suspend",
  reactivate: "Reactivate",
};

const CONFIRM: Record<AdminAction, (email: string) => string> = {
  approve: (e) => `Approve ${e}? They will immediately gain access to job data.`,
  reject: (e) => `Reject ${e}? They will be told their request was not approved.`,
  suspend: (e) => `Suspend ${e}? They will lose access to job data immediately.`,
  reactivate: (e) => `Reactivate ${e}? Their access will be restored.`,
};

const STYLE: Record<AdminAction, string> = {
  approve: "bg-emerald-600 text-white hover:bg-emerald-700",
  reject: "border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-400 dark:hover:bg-red-950",
  suspend: "border border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:hover:bg-slate-800",
  reactivate: "bg-blue-600 text-white hover:bg-blue-700",
};

/** Which decisions make sense from where the user currently is. */
function availableActions(status: AdminUser["status"]): AdminAction[] {
  switch (status) {
    case "PENDING":
      return ["approve", "reject"];
    case "APPROVED":
      return ["suspend"];
    case "SUSPENDED":
      return ["reactivate", "reject"];
    case "REJECTED":
      return ["approve"];
    default:
      return [];
  }
}

export function UserActions({ user }: { user: AdminUser }) {
  const [pending, startTransition] = useTransition();
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);
  const router = useRouter();

  const run = (action: AdminAction) => {
    if (!window.confirm(CONFIRM[action](user.email))) return;
    startTransition(async () => {
      const r = await decideOnUser(user.id, action);
      setResult(r);
      // The server action revalidates; refresh pulls the new rows so the table and the
      // summary cards move together rather than one lagging the other.
      if (r.ok) router.refresh();
    });
  };

  const actions = availableActions(user.status);

  return (
    <div className="flex flex-wrap items-center gap-2">
      {actions.map((action) => (
        <button
          key={action}
          type="button"
          disabled={pending}
          onClick={() => run(action)}
          className={`rounded-lg px-2.5 py-1 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${STYLE[action]}`}
        >
          {pending ? "…" : LABEL[action]}
        </button>
      ))}
      {actions.length === 0 ? <span className="text-xs text-slate-400">—</span> : null}
      {result && !result.ok ? (
        <span role="alert" className="text-xs text-red-600 dark:text-red-400">
          {result.message}
        </span>
      ) : null}
    </div>
  );
}
