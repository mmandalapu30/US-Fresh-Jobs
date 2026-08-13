"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import type { JobSummary } from "@/lib/api";
import {
  type AppliedMap,
  STORAGE_KEY,
  readApplied,
  toSummary,
  writeApplied,
} from "@/lib/applied";

interface AppliedContextValue {
  entries: AppliedMap;
  /** False until localStorage has been read. See the note on the effect below. */
  ready: boolean;
  isApplied: (jobId: number) => boolean;
  apply: (job: JobSummary) => void;
  unapply: (jobId: number) => void;
}

const AppliedContext = createContext<AppliedContextValue | null>(null);

export function AppliedProvider({ children }: { children: React.ReactNode }) {
  const [entries, setEntries] = useState<AppliedMap>({});
  const [ready, setReady] = useState(false);

  // Storage is read in an effect, never during render. The server has no localStorage, so
  // the first client render has to match the server's — nothing applied — or hydration
  // mismatches. Applied jobs therefore leave the feed one paint after mount, not before.
  useEffect(() => {
    setEntries(readApplied());
    setReady(true);

    // Another tab marking a job applied should not leave this one showing it in the feed.
    const sync = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY) setEntries(readApplied());
    };
    window.addEventListener("storage", sync);
    return () => window.removeEventListener("storage", sync);
  }, []);

  const apply = useCallback((job: JobSummary) => {
    setEntries((prev) => {
      // Merged onto what is actually in storage, so a click that lands before the initial
      // read — or after another tab wrote — cannot drop the other entries. The id key is
      // what makes applying twice a no-op instead of a duplicate.
      const next: AppliedMap = {
        ...readApplied(),
        ...prev,
        [job.id]: { job: toSummary(job), applied_at: new Date().toISOString() },
      };
      writeApplied(next);
      return next;
    });
  }, []);

  const unapply = useCallback((jobId: number) => {
    setEntries((prev) => {
      const next: AppliedMap = { ...readApplied(), ...prev };
      delete next[jobId];
      writeApplied(next);
      return next;
    });
  }, []);

  const value = useMemo<AppliedContextValue>(
    () => ({ entries, ready, isApplied: (jobId) => jobId in entries, apply, unapply }),
    [entries, ready, apply, unapply],
  );

  return <AppliedContext.Provider value={value}>{children}</AppliedContext.Provider>;
}

export function useApplied(): AppliedContextValue {
  const value = useContext(AppliedContext);
  if (!value) throw new Error("useApplied must be used inside <AppliedProvider>");
  return value;
}
