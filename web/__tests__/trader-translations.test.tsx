import { describe, expect, it } from "vitest";

import type { AlertItem, AuditLogItem, Signal } from "@/lib/trader-api";
import {
  friendlyTimestamp,
  mergeActivityLog,
  translateAlert,
  translateAuditEvent,
  translateDriftStatus,
  translateSignal,
} from "@/lib/trader-translations";

// ---- drift status mapping --------------------------------------------------

describe("translateDriftStatus", () => {
  it.each<["healthy" | "watch" | "breach" | null, RegExp, string]>([
    ["healthy", /performing as expected/i, "positive"],
    ["watch", /a bit different/i, "accent"],
    ["breach", /not behaving/i, "negative"],
    [null, /no drift data yet/i, "neutral"],
  ])("%s → label matches %s, tone=%s", (status, label, tone) => {
    const out = translateDriftStatus(status);
    expect(out.label).toMatch(label);
    expect(out.tone).toBe(tone);
    expect(out.explanation.length).toBeGreaterThan(0);
  });
});

// ---- audit translator ------------------------------------------------------

function audit(
  event: string,
  payload: Record<string, unknown>,
  overrides: Partial<AuditLogItem> = {},
): AuditLogItem {
  return {
    id: overrides.id ?? 1,
    ts: overrides.ts ?? "2026-05-19T10:00:00Z",
    actor: overrides.actor ?? "bot",
    event,
    entity_type: overrides.entity_type ?? "paper_position",
    entity_id: overrides.entity_id ?? null,
    payload,
  };
}

describe("translateAuditEvent", () => {
  it("renders position_opened with symbol + price + strategy", () => {
    const out = translateAuditEvent(
      audit("position_opened", {
        symbol: "BTC/USDT",
        entry_price: "97420",
        strategy_name: "EMA Trend",
      }),
    );
    expect(out).not.toBeNull();
    expect(out?.text).toMatch(/Bought BTC\/USDT at \$97,420/);
    expect(out?.text).toMatch(/EMA Trend/);
    expect(out?.dotIntent).toBe("neutral");
    expect(out?.source).toBe("audit");
  });

  it("renders position_closed_by_signal with PnL tone", () => {
    const win = translateAuditEvent(
      audit("position_closed_by_signal", { symbol: "ETH/USDT", realised_pnl: "12.40" }),
    );
    expect(win?.text).toMatch(/Closed ETH\/USDT.*\+£12\.40/);
    expect(win?.dotIntent).toBe("positive");

    const loss = translateAuditEvent(
      audit("position_closed_by_signal", { symbol: "ETH/USDT", realised_pnl: "-8.10" }),
    );
    expect(loss?.text).toMatch(/Closed ETH\/USDT/);
    expect(loss?.dotIntent).toBe("negative");
  });

  it("renders stop-hit events as negative", () => {
    const out = translateAuditEvent(
      audit("position_closed_by_stop", { symbol: "BTC/USDT", stop_price: "94800" }),
    );
    expect(out?.text).toMatch(/Stop loss triggered for BTC\/USDT/);
    expect(out?.text).toMatch(/\$94,800/);
    expect(out?.dotIntent).toBe("negative");
  });

  it("renders hold decisions as neutral", () => {
    const out = translateAuditEvent(audit("hold", { symbol: "BTC/USDT" }));
    expect(out?.text).toMatch(/Looked at BTC\/USDT, decided to hold/);
    expect(out?.dotIntent).toBe("neutral");
  });

  it("renders signal_blocked with the reason + accent tone", () => {
    const out = translateAuditEvent(
      audit("signal_blocked", { reason: "stale data" }),
    );
    expect(out?.text).toMatch(/Trade blocked: stale data/);
    expect(out?.dotIntent).toBe("accent");
  });

  it("returns null for unknown event types", () => {
    expect(translateAuditEvent(audit("schema_migrated", {}))).toBeNull();
  });
});

// ---- signal translator -----------------------------------------------------

function signal(kind: Signal["signal"], overrides: Partial<Signal> = {}): Signal {
  return {
    id: overrides.id ?? "s-1",
    strategy_version_id: overrides.strategy_version_id ?? "v-1",
    symbol: overrides.symbol ?? "BTC/USDT",
    timeframe: overrides.timeframe ?? "4h",
    candle_close_ts: overrides.candle_close_ts ?? "2026-05-19T08:00:00Z",
    signal: kind,
    reason: overrides.reason ?? "ma_cross",
    indicators: overrides.indicators ?? {},
    proposed_entry_price: overrides.proposed_entry_price ?? "97420",
    proposed_stop_price: overrides.proposed_stop_price ?? "94800",
    proposed_take_profit_price: overrides.proposed_take_profit_price ?? null,
    created_at: overrides.created_at ?? "2026-05-19T08:01:00Z",
    processed_at: overrides.processed_at ?? null,
  };
}

describe("translateSignal", () => {
  it("renders BUY with the proposed entry price", () => {
    const out = translateSignal(signal("BUY"));
    expect(out?.text).toMatch(/Bought BTC\/USDT at \$97,420/);
    expect(out?.source).toBe("signal");
  });

  it("renders EXIT as a position close", () => {
    const out = translateSignal(signal("EXIT"));
    expect(out?.text).toMatch(/Closed BTC\/USDT position/);
  });

  it("renders HOLD as a hold note", () => {
    const out = translateSignal(signal("HOLD"));
    expect(out?.text).toMatch(/Looked at BTC\/USDT, decided to hold/);
  });
});

// ---- alert translator ------------------------------------------------------

function alert(
  subject: string,
  body: string,
  overrides: Partial<AlertItem> = {},
): AlertItem {
  return {
    id: overrides.id ?? "a-1",
    ts: overrides.ts ?? "2026-05-19T00:05:00Z",
    channel: overrides.channel ?? "telegram",
    severity: overrides.severity ?? "info",
    subject,
    body,
    delivered: overrides.delivered ?? false,
    delivery_error: overrides.delivery_error ?? null,
  };
}

describe("translateAlert", () => {
  it("renders daily summary as neutral", () => {
    const out = translateAlert(alert("Daily summary", "balance £1023.45 (+2.3% today)"));
    expect(out?.text).toMatch(/Daily summary/);
    expect(out?.dotIntent).toBe("neutral");
  });

  it("renders phase-failure alerts as negative + names the phase", () => {
    const out = translateAlert(
      alert("Trader phase failing: signal", "Phase 'signal' failed 3 consecutive cycles."),
    );
    expect(out?.text).toMatch(/Bot ran into a problem.*phase: signal/);
    expect(out?.dotIntent).toBe("negative");
  });

  it("renders phase-recovery as positive", () => {
    const out = translateAlert(
      alert("Trader phase recovered: signal", "Phase 'signal' succeeded after 4 failures."),
    );
    expect(out?.text).toMatch(/Recovered from an earlier problem/);
    expect(out?.dotIntent).toBe("positive");
  });

  it("renders stale-heartbeat alerts as negative", () => {
    const out = translateAlert(
      alert("Trader run crashed (stale heartbeat)", "Run … (loop=runner) had no heartbeat …"),
    );
    expect(out?.text).toMatch(/heartbeat went silent/);
    expect(out?.dotIntent).toBe("negative");
  });

  it("falls back to subject + body for unrecognised alert kinds", () => {
    const out = translateAlert(
      alert("Custom thing", "details here", { severity: "warning" }),
    );
    expect(out?.text).toMatch(/Custom thing: details here/);
    expect(out?.dotIntent).toBe("accent");
  });
});

// ---- merge: the load-bearing test for adjustment 2 -------------------------

describe("mergeActivityLog (cross-source chronological ordering)", () => {
  it("normalises ts / created_at across the three sources and sorts desc", () => {
    // Three sources with different timestamp field names, interleaved.
    const a = audit("position_opened", { symbol: "BTC/USDT", entry_price: "100" }, {
      id: 1,
      ts: "2026-05-19T10:30:00Z",
    });
    const b = signal("BUY", {
      id: "sig-2",
      created_at: "2026-05-19T11:15:00Z",
    });
    const c = alert("Daily summary", "today", {
      id: "alt-3",
      ts: "2026-05-19T09:05:00Z",
    });
    const d = audit("position_closed_by_signal", { symbol: "BTC/USDT", realised_pnl: "10" }, {
      id: 4,
      ts: "2026-05-19T11:45:00Z",
    });

    const merged = mergeActivityLog({ audit: [a, d], signals: [b], alerts: [c] });

    // Expected order, newest first: d (11:45), b (11:15), a (10:30), c (09:05).
    expect(merged.map((e) => e.key)).toEqual([
      "audit-4",
      "signal-sig-2",
      "audit-1",
      "alert-alt-3",
    ]);
    // tsMs is monotonically non-increasing.
    for (let i = 1; i < merged.length; i++) {
      expect(merged[i]!.tsMs).toBeLessThanOrEqual(merged[i - 1]!.tsMs);
    }
  });

  it("drops entries whose translator returns null", () => {
    const unknown = audit("schema_migrated", {}, { id: 99, ts: "2026-05-19T12:00:00Z" });
    const known = audit("position_opened", { symbol: "BTC/USDT", entry_price: "1" }, {
      id: 1,
      ts: "2026-05-19T11:00:00Z",
    });
    const merged = mergeActivityLog({ audit: [unknown, known], signals: [], alerts: [] });
    expect(merged).toHaveLength(1);
    expect(merged[0]!.key).toBe("audit-1");
  });

  it("truncates to the requested limit", () => {
    const rows = Array.from({ length: 25 }, (_, i) =>
      audit(
        "position_opened",
        { symbol: "BTC/USDT", entry_price: "1" },
        { id: i, ts: `2026-05-19T${String(i).padStart(2, "0")}:00:00Z` },
      ),
    );
    const merged = mergeActivityLog({ audit: rows, signals: [], alerts: [] }, 20);
    expect(merged).toHaveLength(20);
    // Newest first — id 24 then 23 ...
    expect(merged[0]!.key).toBe("audit-24");
    expect(merged[19]!.key).toBe("audit-5");
  });
});

describe("friendlyTimestamp", () => {
  const now = Date.parse("2026-05-19T15:00:00Z");

  it("renders same-day timestamps as HH:MM (UTC)", () => {
    const ts = Date.parse("2026-05-19T09:30:00Z");
    expect(friendlyTimestamp(ts, now)).toBe("09:30");
  });

  it("renders yesterday timestamps with the prefix", () => {
    const ts = Date.parse("2026-05-18T20:00:00Z");
    expect(friendlyTimestamp(ts, now)).toMatch(/^Yesterday 20:00$/);
  });

  it("renders older timestamps with day + month", () => {
    const ts = Date.parse("2026-05-10T12:30:00Z");
    expect(friendlyTimestamp(ts, now)).toMatch(/10 May.*12:30/);
  });
});
