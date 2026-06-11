import { describe, expect, it } from "vitest";

import {
  classifyBotStatus,
  classifyMarketData,
  classifySafety,
  classifyStrategies,
  type StatusLevel,
} from "@/lib/trader-status";

const NOW = 1_700_000_000_000; // arbitrary fixed clock

describe("classifyBotStatus", () => {
  it("returns neutral when there is no bot run at all", () => {
    const level = classifyBotStatus({
      runStatus: null,
      lastHeartbeatMs: null,
      lastSnapshotMs: null,
      nowMs: NOW,
    });
    expect(level).toBe<StatusLevel>("neutral");
  });

  it("returns red when the latest run is crashed", () => {
    expect(
      classifyBotStatus({
        runStatus: "crashed",
        lastHeartbeatMs: NOW - 30_000, // fresh heartbeat doesn't matter
        lastSnapshotMs: NOW - 30_000,
        nowMs: NOW,
      }),
    ).toBe("red");
  });

  it("returns neutral when a previous run is cleanly stopped", () => {
    expect(
      classifyBotStatus({
        runStatus: "stopped",
        lastHeartbeatMs: NOW - 60 * 60 * 1000,
        lastSnapshotMs: NOW - 60 * 60 * 1000,
        nowMs: NOW,
      }),
    ).toBe("neutral");
  });

  describe("heartbeat freshness thresholds (90s / 5min)", () => {
    it.each<[number, StatusLevel]>([
      [0, "green"],
      [89 * 1000, "green"],
      [90 * 1000, "green"], // boundary inclusive
      [91 * 1000, "yellow"],
      [4 * 60 * 1000, "yellow"],
      [5 * 60 * 1000, "yellow"], // boundary inclusive
      [5 * 60 * 1000 + 1, "red"],
      [60 * 60 * 1000, "red"],
    ])("heartbeat age %s ms → %s (snapshot fresh)", (ageMs, expected) => {
      const level = classifyBotStatus({
        runStatus: "running",
        lastHeartbeatMs: NOW - ageMs,
        lastSnapshotMs: NOW, // snapshot is fresh
        nowMs: NOW,
      });
      expect(level).toBe(expected);
    });
  });

  it("uses the WORST of heartbeat + snapshot freshness", () => {
    // heartbeat fresh, snapshot 6 minutes old → red.
    const level = classifyBotStatus({
      runStatus: "running",
      lastHeartbeatMs: NOW - 10_000,
      lastSnapshotMs: NOW - 6 * 60 * 1000,
      nowMs: NOW,
    });
    expect(level).toBe("red");
  });

  it("returns red when snapshot has never been written under a running run", () => {
    const level = classifyBotStatus({
      runStatus: "running",
      lastHeartbeatMs: NOW - 10_000,
      lastSnapshotMs: null,
      nowMs: NOW,
    });
    expect(level).toBe("red");
  });
});

describe("classifyMarketData", () => {
  it("returns neutral on null age", () => {
    expect(
      classifyMarketData({ worstPairAgeMs: null, staleThresholdMs: 600 * 1000 }),
    ).toBe("neutral");
  });

  it.each<[number, number, StatusLevel]>([
    [0, 600 * 1000, "green"],
    [600 * 1000, 600 * 1000, "green"], // == threshold → still fresh
    [600 * 1000 + 1, 600 * 1000, "yellow"],
    [1200 * 1000, 600 * 1000, "yellow"], // 2× threshold → still yellow
    [1200 * 1000 + 1, 600 * 1000, "red"],
  ])("age=%s threshold=%s → %s", (age, threshold, expected) => {
    expect(
      classifyMarketData({ worstPairAgeMs: age, staleThresholdMs: threshold }),
    ).toBe(expected);
  });
});

describe("classifySafety", () => {
  it("returns green when no events recently", () => {
    expect(classifySafety({ recentEventSeverities: [] })).toBe("green");
  });

  it("returns green when only info severity present", () => {
    expect(classifySafety({ recentEventSeverities: ["info", "info"] })).toBe("green");
  });

  it("returns yellow when any warning is present (no critical)", () => {
    expect(
      classifySafety({ recentEventSeverities: ["info", "warning", "info"] }),
    ).toBe("yellow");
  });

  it("returns red when any critical is present", () => {
    expect(
      classifySafety({ recentEventSeverities: ["warning", "critical", "info"] }),
    ).toBe("red");
  });
});

describe("classifyStrategies", () => {
  it("returns neutral when no strategies given", () => {
    expect(classifyStrategies({ driftHealths: [] })).toBe("neutral");
  });

  it("returns green when all healthy / null", () => {
    expect(
      classifyStrategies({ driftHealths: ["healthy", null, "healthy"] }),
    ).toBe("green");
  });

  it("returns yellow when any on watch (no breach)", () => {
    expect(
      classifyStrategies({ driftHealths: ["healthy", "watch", null] }),
    ).toBe("yellow");
  });

  it("returns red when any in breach", () => {
    expect(
      classifyStrategies({ driftHealths: ["watch", "breach", "healthy"] }),
    ).toBe("red");
  });
});
