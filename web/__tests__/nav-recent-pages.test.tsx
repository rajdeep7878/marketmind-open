import { beforeEach, describe, expect, it } from "vitest";

import {
  _clearRecentPages,
  readRecentPages,
  recordRecentPage,
} from "@/lib/recent-pages";

describe("recent pages localStorage", () => {
  beforeEach(() => {
    _clearRecentPages();
  });

  it("starts empty", () => {
    expect(readRecentPages()).toEqual([]);
  });

  it("records a new page", () => {
    recordRecentPage({ path: "/trader", title: "Trader dashboard" });
    const out = readRecentPages();
    expect(out).toHaveLength(1);
    expect(out[0]!.path).toBe("/trader");
    expect(out[0]!.title).toBe("Trader dashboard");
    expect(typeof out[0]!.viewedAt).toBe("number");
  });

  it("excludes the home path", () => {
    recordRecentPage({ path: "/", title: "MarketMind" });
    expect(readRecentPages()).toEqual([]);
  });

  it("dedupes by path — re-recording moves to position 0", async () => {
    recordRecentPage({ path: "/extract", title: "Analyse" });
    // Tiny pause so viewedAt is monotonic and re-record is strictly later.
    await new Promise((r) => setTimeout(r, 2));
    recordRecentPage({ path: "/trader", title: "Trader" });
    await new Promise((r) => setTimeout(r, 2));
    recordRecentPage({ path: "/extract", title: "Analyse v2" });

    const out = readRecentPages();
    expect(out).toHaveLength(2);
    expect(out[0]!.path).toBe("/extract");
    expect(out[0]!.title).toBe("Analyse v2"); // title refreshed
    expect(out[1]!.path).toBe("/trader");
  });

  it("caps at 5 entries (drops oldest)", () => {
    for (let i = 0; i < 8; i++) {
      recordRecentPage({ path: `/p${i}`, title: `Page ${i}` });
    }
    const out = readRecentPages();
    expect(out).toHaveLength(5);
    // Newest is /p7, oldest in the kept window is /p3.
    expect(out[0]!.path).toBe("/p7");
    expect(out[4]!.path).toBe("/p3");
  });

  it("sorts newest-first", async () => {
    recordRecentPage({ path: "/a", title: "A" });
    await new Promise((r) => setTimeout(r, 2));
    recordRecentPage({ path: "/b", title: "B" });
    await new Promise((r) => setTimeout(r, 2));
    recordRecentPage({ path: "/c", title: "C" });
    const out = readRecentPages();
    expect(out.map((r) => r.path)).toEqual(["/c", "/b", "/a"]);
  });

  it("returns [] on JSON parse failure", () => {
    window.localStorage.setItem("marketmind-recent-pages", "not-json{");
    expect(readRecentPages()).toEqual([]);
  });

  it("returns [] on schema-violating entries", () => {
    window.localStorage.setItem(
      "marketmind-recent-pages",
      JSON.stringify([{ path: "/p", title: 123 /* wrong */ }]),
    );
    expect(readRecentPages()).toEqual([]);
  });
});
