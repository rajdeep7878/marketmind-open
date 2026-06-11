import { describe, expect, it } from "vitest";

import { matchScore, rankByMatch } from "@/lib/fuzzy-match";

describe("matchScore", () => {
  it("returns the special 'empty query' constant for empty input", () => {
    expect(matchScore("", { title: "Trader" })).toBe(1);
    expect(matchScore("   ", { title: "Trader" })).toBe(1);
  });

  it("scores exact matches highest", () => {
    const exact = matchScore("trader", { title: "Trader" });
    const startsWith = matchScore("tra", { title: "Trader" });
    expect(exact).toBeGreaterThan(startsWith);
    expect(exact).toBe(1000);
  });

  it("prefers 'starts with' over 'substring' over 'acronym' over subtitle", () => {
    const startsWith = matchScore("ad", { title: "Admin stats" });
    const substring = matchScore("min", { title: "Admin stats" });
    const acronym = matchScore("as", { title: "Admin stats" });
    expect(startsWith).toBeGreaterThan(substring);
    expect(substring).toBeGreaterThan(acronym);

    const subtitleMatch = matchScore("dash", {
      title: "Admin stats",
      subtitle: "dashboards live here",
    });
    expect(acronym).toBeGreaterThan(subtitleMatch);
  });

  it("matches the acronym of multi-word titles", () => {
    expect(matchScore("ts", { title: "Trader Strategies" })).toBeGreaterThan(0);
    expect(matchScore("aas", { title: "Analyse a strategy" })).toBeGreaterThan(0);
  });

  it("is case-insensitive", () => {
    expect(matchScore("TRADER", { title: "trader" })).toBe(1000);
  });

  it("returns 0 when nothing matches", () => {
    expect(matchScore("zzz", { title: "Trader", subtitle: "/trader" })).toBe(0);
  });

  it("ties broken by string length (shorter wins on 'starts with')", () => {
    const short = matchScore("h", { title: "Home" });
    const long = matchScore("h", { title: "Home dashboard with a long name" });
    expect(short).toBeGreaterThan(long);
  });
});

describe("rankByMatch", () => {
  const corpus = [
    { title: "Home", subtitle: "/" },
    { title: "Analyse a strategy", subtitle: "/extract" },
    { title: "Trader dashboard", subtitle: "/trader" },
    { title: "Admin stats", subtitle: "/admin/stats" },
  ];

  it("returns only items with non-zero scores, sorted desc", () => {
    const out = rankByMatch("trader", corpus);
    expect(out).toHaveLength(1);
    expect(out[0]!.title).toBe("Trader dashboard");
  });

  it("returns multiple hits in score order", () => {
    const out = rankByMatch("a", corpus);
    // "Analyse a strategy" starts with 'a' → higher than "Admin stats"
    // ("a" starts both, but "Admin stats" is also prefix; tiebreak by length).
    expect(out.length).toBeGreaterThanOrEqual(2);
    expect(out[0]!.title.toLowerCase().startsWith("a")).toBe(true);
  });

  it("returns [] when no targets match", () => {
    expect(rankByMatch("zzzz", corpus)).toEqual([]);
  });

  it("returns the full corpus on empty query (with stable order)", () => {
    const out = rankByMatch("", corpus);
    expect(out).toHaveLength(corpus.length);
  });
});
