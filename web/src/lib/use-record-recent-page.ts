"use client";

import { useEffect } from "react";

import { recordRecentPage } from "@/lib/recent-pages";

/**
 * Page-mount side effect: write the current page into the
 * `marketmind-recent-pages` localStorage list. Each page calls
 * this once at the top of its function body.
 *
 * The hook intentionally re-fires when `title` or `path` change —
 * useful on dynamic routes where the human-readable title arrives
 * asynchronously (e.g. a strategy name fetched after the page
 * mounts). The recent-pages dedupe-by-path semantics handle that
 * — a second call with the same path just refreshes the title.
 *
 * Home path "/" is silently filtered by recordRecentPage; the
 * landing page can still call this for symmetry without
 * polluting the recent list.
 */
export function useRecordRecentPage(entry: { path: string; title: string }): void {
  // Destructured so the eslint exhaustive-deps rule accepts the
  // primitive dependencies. Re-creating the object inside the
  // effect avoids depending on `entry` identity.
  const { path, title } = entry;
  useEffect(() => {
    recordRecentPage({ path, title });
  }, [path, title]);
}

/**
 * Client-island wrapper around the hook for server-component
 * pages (e.g. /admin/stats). Server pages can render
 * `<RecordRecentPage path="/admin/stats" title="Admin stats" />`
 * to opt into the recent-pages tracking without converting the
 * whole page to a client component.
 */
export function RecordRecentPage(entry: {
  path: string;
  title: string;
}): null {
  useRecordRecentPage(entry);
  return null;
}
