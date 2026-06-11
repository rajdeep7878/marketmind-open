/**
 * Tiny fuzzy matcher for the command palette.
 *
 * Designed for a ~10-item corpus (page list + a handful of
 * actions). Trades off the linear-scan cost of a real fuzzy
 * library for ~1 kB of code instead of fuse.js's ~9 kB.
 *
 * Scoring (higher is better; 0 means no match):
 *   1000  → exact case-insensitive match of the title
 *    500  → title starts with the query
 *    250  → query is a substring of the title
 *    200  → query matches the title's acronym (first letters of
 *           whitespace-separated words). "ts" matches "Trader
 *           Strategies".
 *    100  → query is a substring of the subtitle
 *      0  → no match
 *
 * Ties are broken by string length (shorter title wins) so that
 * "Home" beats "Home dashboard" for a single-character query.
 */

export interface FuzzyTarget {
  title: string;
  subtitle?: string | null | undefined;
}

export function matchScore(query: string, target: FuzzyTarget): number {
  const q = query.trim().toLowerCase();
  if (q.length === 0) return 1; // empty query — surface everything with stable score
  const title = target.title.toLowerCase();

  if (title === q) return 1000;
  if (title.startsWith(q)) return 500 - Math.min(target.title.length, 100);
  if (title.includes(q)) return 250 - Math.min(target.title.length, 100);

  // Acronym match: take the first letter of each whitespace-
  // separated word, then test equality / prefix with the query.
  const acronym = target.title
    .split(/\s+/)
    .map((w) => w[0]?.toLowerCase() ?? "")
    .join("");
  if (acronym === q || acronym.startsWith(q)) {
    return 200 - Math.min(target.title.length, 100);
  }

  const subtitle = (target.subtitle ?? "").toLowerCase();
  if (subtitle.length > 0 && subtitle.includes(q)) {
    return 100 - Math.min(subtitle.length, 100);
  }

  return 0;
}

/**
 * Rank an iterable of targets by match score, descending. Returns
 * the entries with score > 0; empty input or no matches → [].
 */
export function rankByMatch<T extends FuzzyTarget>(
  query: string,
  targets: ReadonlyArray<T>,
): T[] {
  return targets
    .map((t) => ({ t, score: matchScore(query, t) }))
    .filter((row) => row.score > 0)
    .sort((a, b) => b.score - a.score)
    .map((row) => row.t);
}
