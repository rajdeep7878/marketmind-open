"use client";

import { StatusDot } from "@/app/trader/components/status-dot";
import { Skeleton } from "@/components/ui/skeleton";
import { traderFetch } from "@/lib/trader-api";
import type { AlertList, AuditList, SignalList } from "@/lib/trader-api";
import {
  friendlyTimestamp,
  mergeActivityLog,
  type DotIntent,
} from "@/lib/trader-translations";
import { usePolling } from "@/lib/use-polling";

/**
 * Section 5 — activity log.
 *
 * Merges three sources client-side. Each source returns rows with a
 * different timestamp field name — `mergeActivityLog` normalises to
 * a single `tsMs` sort key, drops entries whose translator returns
 * null, and truncates to the most recent 20.
 *
 * One row per event. The row's coloured dot signals tone (positive /
 * negative / accent / neutral); the text is plain English.
 */

export function ActivityLogSection({
  onLastUpdated,
}: {
  onLastUpdated: (_d: Date) => void;
}): React.ReactElement {
  const audit = usePolling<AuditList>({
    fetcher: (signal) => traderFetch<AuditList>("/trader/audit/recent?limit=50", signal),
  });
  const signals = usePolling<SignalList>({
    fetcher: (signal) => traderFetch<SignalList>("/trader/signals/recent?limit=20", signal),
  });
  const alerts = usePolling<AlertList>({
    fetcher: (signal) => traderFetch<AlertList>("/trader/alerts/recent?limit=20", signal),
  });

  for (const d of [audit.lastUpdatedAt, signals.lastUpdatedAt, alerts.lastUpdatedAt]) {
    if (d) onLastUpdated(d);
  }

  if (audit.isLoading && signals.isLoading && alerts.isLoading) {
    return (
      <section>
        <h2 className="font-serif text-2xl text-ink">Activity</h2>
        <div className="mt-6 flex flex-col gap-2">
          {[0, 1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-7 w-full" />
          ))}
        </div>
      </section>
    );
  }

  // Tolerate partial failures: any source that errors contributes
  // zero rows, but the others still render.
  const entries = mergeActivityLog({
    audit: audit.data?.items ?? [],
    signals: signals.data?.items ?? [],
    alerts: alerts.data?.items ?? [],
  });

  return (
    <section>
      <h2 className="font-serif text-2xl text-ink">Activity</h2>
      {entries.length === 0 ? (
        <p className="mt-4 rounded-sm border border-hairline p-4 text-sm text-muted">
          No activity yet.
        </p>
      ) : (
        <ul className="mt-6 flex flex-col divide-y divide-hairline">
          {entries.map((e) => (
            <li key={e.key} className="flex items-baseline gap-3 py-2.5">
              <StatusDot level={intentToLevel(e.dotIntent)} size="sm" />
              <span className="font-mono text-xs tabular-nums text-muted">
                {friendlyTimestamp(e.tsMs)}
              </span>
              <span className="text-sm leading-snug text-ink">{e.text}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function intentToLevel(i: DotIntent): "green" | "yellow" | "red" | "neutral" {
  switch (i) {
    case "positive":
      return "green";
    case "accent":
      return "yellow";
    case "negative":
      return "red";
    case "neutral":
      return "neutral";
  }
}
