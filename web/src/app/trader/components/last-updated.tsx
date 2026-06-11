"use client";

import { useEffect, useState } from "react";

/**
 * "Last updated 12s ago" indicator pinned to the page corner.
 *
 * Tick-driven (1s interval) so the value updates without requiring
 * the parent to re-render on every refresh. Renders nothing if no
 * timestamp has been recorded yet — the first successful poll
 * brings it to life.
 */
interface LastUpdatedProps {
  ts: Date | null;
}

function formatAgo(ts: Date, now: Date): string {
  const sec = Math.max(0, Math.round((now.getTime() - ts.getTime()) / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}

export function LastUpdated({ ts }: LastUpdatedProps): React.ReactElement | null {
  const [now, setNow] = useState<Date>(() => new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  if (ts === null) return null;
  return (
    <span className="font-mono text-xs tabular-nums text-muted">
      Last updated {formatAgo(ts, now)}
    </span>
  );
}
