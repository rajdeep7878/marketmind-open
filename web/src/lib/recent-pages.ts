/**
 * localStorage-backed "recent pages" list for the command palette.
 *
 * Storage shape: `marketmind-recent-pages` is a JSON array of
 *   { path, title, viewedAt }
 * records, sorted newest-first, capped at 5, deduped by path.
 *
 * Behaviour:
 *   - Re-recording an existing path moves it to position 0 (most
 *     recent) and refreshes its title — so a strategy's name
 *     change between visits is reflected on the next palette open.
 *   - The home path "/" is excluded — already reachable via the
 *     "MarketMind" segment in every page's breadcrumb.
 *   - All reads/writes are wrapped in try/catch; private-browsing
 *     and storage-quota failures cause the palette to silently
 *     show no RECENT section.
 */

const STORAGE_KEY = "marketmind-recent-pages";
const MAX_ENTRIES = 5;

export interface RecentPage {
  path: string;
  title: string;
  viewedAt: number;
}

function isRecentPage(value: unknown): value is RecentPage {
  if (value === null || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.path === "string" &&
    typeof v.title === "string" &&
    typeof v.viewedAt === "number"
  );
}

export function readRecentPages(): RecentPage[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isRecentPage);
  } catch {
    return [];
  }
}

/**
 * Insert (or move-to-top) the given page. Returns the resulting
 * array — useful for tests; the production caller normally
 * ignores the return value.
 *
 * The home path "/" is silently dropped because it's always
 * reachable via the breadcrumb.
 */
export function recordRecentPage(entry: {
  path: string;
  title: string;
}): RecentPage[] {
  if (entry.path === "/" || entry.path.length === 0) {
    return readRecentPages();
  }
  const existing = readRecentPages().filter((p) => p.path !== entry.path);
  const next: RecentPage[] = [
    { path: entry.path, title: entry.title, viewedAt: Date.now() },
    ...existing,
  ].slice(0, MAX_ENTRIES);
  if (typeof window === "undefined") return next;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // private browsing / quota — palette silently degrades.
  }
  return next;
}

/** Test-only escape hatch. Not exported from any UI module. */
export function _clearRecentPages(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
