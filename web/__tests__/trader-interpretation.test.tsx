import { describe, expect, it } from "vitest";

import {
  computeTodayDelta,
  interpretEquityPerformance,
} from "@/lib/trader-interpretation";

describe("interpretEquityPerformance", () => {
  describe("less than 7 days of data", () => {
    it.each([0, 1, 3, 6, 6.9])("days=%s renders the 'too early' branch", (days) => {
      const text = interpretEquityPerformance({
        currentEquity: 1234,
        startingCash: 1000,
        daysElapsed: days,
      });
      expect(text).toMatch(/too early/i);
      // The day-count rendered must match the rounded input.
      expect(text).toContain(String(Math.round(days)));
    });

    it("singularises 'day' for daysElapsed=1", () => {
      const text = interpretEquityPerformance({
        currentEquity: 1000,
        startingCash: 1000,
        daysElapsed: 1,
      });
      expect(text).toMatch(/1 day\b/);
      expect(text).not.toMatch(/1 days\b/);
    });
  });

  describe("profit branch (>= 7 days, positive)", () => {
    it("includes the magnitude and a per-month estimate", () => {
      // 5% over 30 days → ~5% per month
      const text = interpretEquityPerformance({
        currentEquity: 1050,
        startingCash: 1000,
        daysElapsed: 30,
      });
      expect(text).toMatch(/made £50\.00/);
      expect(text).toMatch(/30 days/);
      // (50 / 1000 / 30) * 30 * 100 = 5.0
      expect(text).toMatch(/5\.0% per month/);
    });

    it("at zero gain still renders the profit branch (delta == 0)", () => {
      const text = interpretEquityPerformance({
        currentEquity: 1000,
        startingCash: 1000,
        daysElapsed: 14,
      });
      expect(text).toMatch(/made £0\.00/);
    });
  });

  describe("loss branch (>= 7 days, negative)", () => {
    it("renders the 'down £X' reassurance line", () => {
      const text = interpretEquityPerformance({
        currentEquity: 920,
        startingCash: 1000,
        daysElapsed: 21,
      });
      expect(text).toMatch(/down £80\.00/);
      expect(text).toMatch(/21 days/);
      expect(text).toMatch(/normal/i);
    });
  });
});

describe("computeTodayDelta", () => {
  it("returns null when no start-of-day snapshot exists", () => {
    expect(computeTodayDelta(1050, null)).toBeNull();
  });

  it("returns null when start equity is zero or negative", () => {
    expect(computeTodayDelta(1050, 0)).toBeNull();
    expect(computeTodayDelta(1050, -10)).toBeNull();
  });

  it("computes a positive delta + pct", () => {
    const d = computeTodayDelta(1050, 1000);
    expect(d).not.toBeNull();
    expect(d?.amount).toBeCloseTo(50);
    expect(d?.pct).toBeCloseTo(5);
    expect(d?.tone).toBe("positive");
  });

  it("computes a negative delta + pct", () => {
    const d = computeTodayDelta(975, 1000);
    expect(d).not.toBeNull();
    expect(d?.amount).toBeCloseTo(-25);
    expect(d?.pct).toBeCloseTo(-2.5);
    expect(d?.tone).toBe("negative");
  });

  it("reports neutral tone when amount is exactly zero", () => {
    const d = computeTodayDelta(1000, 1000);
    expect(d?.tone).toBe("neutral");
  });
});
