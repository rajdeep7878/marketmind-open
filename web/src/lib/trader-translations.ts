/**
 * Pure functions that translate raw trader events to plain-English
 * dashboard rows.
 *
 * Each translator returns a unified `ActivityEntry` shape so the
 * activity-log section can merge audit + signal + alert sources
 * and sort by tsMs.
 */

import type { AlertItem, AuditLogItem, Signal } from "@/lib/trader-api";

export type DotIntent = "neutral" | "positive" | "negative" | "accent";

export interface ActivityEntry {
  key: string;
  tsMs: number;
  text: string;
  dotIntent: DotIntent;
  source: "audit" | "signal" | "alert";
}

// ---- Drift status ---------------------------------------------------------

export interface DriftLabel {
  label: string;
  tone: "positive" | "accent" | "negative" | "neutral";
  explanation: string;
}

export function translateDriftStatus(
  status: "healthy" | "watch" | "breach" | null,
): DriftLabel {
  switch (status) {
    case "healthy":
      return {
        label: "Performing as expected",
        tone: "positive",
        explanation: "Live performance is within tolerance of the approving backtest.",
      };
    case "watch":
      return {
        label: "A bit different from backtest — keep watching",
        tone: "accent",
        explanation: "Live performance is drifting; not yet a breach but worth monitoring.",
      };
    case "breach":
      return {
        label: "Not behaving like the backtest — consider pausing",
        tone: "negative",
        explanation: "Live performance is materially different from the approving backtest.",
      };
    case null:
      return {
        label: "No drift data yet",
        tone: "neutral",
        explanation: "Drift evaluation runs daily at 01:00 UTC; first check appears after that.",
      };
  }
}

// ---- Numeric formatting helpers (shared by translators) -------------------

function fmtPrice(raw: string | null): string {
  if (raw === null) return "—";
  const n = Number.parseFloat(raw);
  if (!Number.isFinite(n)) return raw;
  // Six-digit prices (e.g. BTC at 97,420) read better with thousands.
  if (n >= 1000) {
    return `$${n.toLocaleString("en-US", { maximumFractionDigits: 0 })}`;
  }
  return `$${n.toFixed(n >= 100 ? 2 : 4)}`;
}

function fmtGbp(raw: string | number | null): string {
  if (raw === null) return "—";
  const n = typeof raw === "number" ? raw : Number.parseFloat(raw);
  if (!Number.isFinite(n)) return String(raw);
  const sign = n >= 0 ? "+" : "−";
  return `${sign}£${Math.abs(n).toFixed(2)}`;
}

function tsToMs(ts: string): number {
  // Parse as UTC; trader timestamps are always tz-aware ISO strings.
  return Date.parse(ts);
}

// ---- Audit event translator ----------------------------------------------

const AUDIT_TRANSLATORS: Record<
  string,
  (_row: AuditLogItem) => { text: string; dot: DotIntent }
> = {
  position_opened: (row) => {
    const symbol = readPayloadString(row.payload, "symbol") ?? row.entity_type;
    const price = readPayloadString(row.payload, "entry_price");
    const priceStr = price ? ` at ${fmtPrice(price)}` : "";
    const strat = readPayloadString(row.payload, "strategy_name");
    const stratStr = strat ? ` (${strat})` : "";
    return {
      text: `Bought ${symbol}${priceStr}${stratStr}`,
      dot: "neutral",
    };
  },
  position_closed_by_signal: (row) => {
    const symbol = readPayloadString(row.payload, "symbol") ?? row.entity_type;
    const pnlRaw = readPayloadString(row.payload, "realised_pnl");
    const pnlNum = pnlRaw ? Number.parseFloat(pnlRaw) : Number.NaN;
    const dot: DotIntent = Number.isFinite(pnlNum) ? (pnlNum >= 0 ? "positive" : "negative") : "neutral";
    return {
      text: `Closed ${symbol} position${Number.isFinite(pnlNum) ? ` for ${fmtGbp(pnlNum)}` : ""}`,
      dot,
    };
  },
  position_closed_by_stop: (row) => {
    const symbol = readPayloadString(row.payload, "symbol") ?? row.entity_type;
    const price = readPayloadString(row.payload, "stop_price");
    const priceStr = price ? ` — closed at ${fmtPrice(price)}` : "";
    return {
      text: `Stop loss triggered for ${symbol}${priceStr}`,
      dot: "negative",
    };
  },
  hold: (row) => {
    const symbol = readPayloadString(row.payload, "symbol") ?? row.entity_type;
    return { text: `Looked at ${symbol}, decided to hold`, dot: "neutral" };
  },
  signal_blocked: (row) => {
    const reason = readPayloadString(row.payload, "reason") ?? "policy";
    return { text: `Trade blocked: ${reason}`, dot: "accent" };
  },
};

function readPayloadString(payload: Record<string, unknown>, key: string): string | null {
  const v = payload[key];
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  return null;
}

export function translateAuditEvent(row: AuditLogItem): ActivityEntry | null {
  const fn = AUDIT_TRANSLATORS[row.event];
  if (!fn) return null;
  const { text, dot } = fn(row);
  return {
    key: `audit-${row.id}`,
    tsMs: tsToMs(row.ts),
    text,
    dotIntent: dot,
    source: "audit",
  };
}

// ---- Signal translator ---------------------------------------------------

export function translateSignal(row: Signal): ActivityEntry | null {
  let text: string;
  let dot: DotIntent = "neutral";
  switch (row.signal) {
    case "BUY":
      text = `Bought ${row.symbol} at ${fmtPrice(row.proposed_entry_price)}`;
      break;
    case "SELL":
      // v1 templates don't emit SELL; defensive fallback.
      text = `Closed ${row.symbol} position`;
      break;
    case "EXIT":
      text = `Closed ${row.symbol} position`;
      break;
    case "HOLD":
      // HOLDs aren't persisted to trader_signals; if one shows up,
      // render minimally.
      text = `Looked at ${row.symbol}, decided to hold`;
      break;
  }
  return {
    key: `signal-${row.id}`,
    tsMs: tsToMs(row.created_at),
    text,
    dotIntent: dot,
    source: "signal",
  };
}

// ---- Alert translator ----------------------------------------------------

export function translateAlert(row: AlertItem): ActivityEntry | null {
  const subjectLower = row.subject.toLowerCase();
  let text: string;
  let dot: DotIntent;

  if (subjectLower.includes("daily summary")) {
    text = `Daily summary — ${row.body.slice(0, 160)}`;
    dot = "neutral";
  } else if (subjectLower.includes("weekly summary")) {
    text = `Weekly summary — ${row.body.slice(0, 160)}`;
    dot = "neutral";
  } else if (subjectLower.includes("phase failing")) {
    const phase = /'([^']+)'/u.exec(row.body)?.[1] ?? "unknown";
    text = `Bot ran into a problem (phase: ${phase}) — retrying`;
    dot = "negative";
  } else if (subjectLower.includes("phase recovered")) {
    text = "Recovered from an earlier problem";
    dot = "positive";
  } else if (subjectLower.includes("stale heartbeat")) {
    text = "Bot heartbeat went silent — runner may have stalled";
    dot = "negative";
  } else if (subjectLower.includes("data_feed")) {
    text = `Market-data issue: ${row.body.slice(0, 120)}`;
    dot = "negative";
  } else {
    // Generic fallback so we never drop an alert from the log.
    text = `${row.subject}: ${row.body.slice(0, 160)}`;
    dot = row.severity === "critical" ? "negative" : row.severity === "warning" ? "accent" : "neutral";
  }
  return {
    key: `alert-${row.id}`,
    tsMs: tsToMs(row.ts),
    text,
    dotIntent: dot,
    source: "alert",
  };
}

// ---- Merge -----------------------------------------------------------------

export interface MergeInputs {
  audit: AuditLogItem[];
  signals: Signal[];
  alerts: AlertItem[];
}

/**
 * Merge audit + signal + alert entries into one chronological list.
 *
 * The three source endpoints use different timestamp field names
 * (`ts` on audit/alerts, `created_at` on signals). We normalise to
 * a unified ms-epoch UTC `tsMs` field on every entry so the sort
 * is uniform. Entries whose translator returns null are dropped.
 *
 * Sort is descending (newest first), then truncated to `limit`.
 */
export function mergeActivityLog(inputs: MergeInputs, limit = 20): ActivityEntry[] {
  const out: ActivityEntry[] = [];
  for (const row of inputs.audit) {
    const e = translateAuditEvent(row);
    if (e) out.push(e);
  }
  for (const row of inputs.signals) {
    const e = translateSignal(row);
    if (e) out.push(e);
  }
  for (const row of inputs.alerts) {
    const e = translateAlert(row);
    if (e) out.push(e);
  }
  out.sort((a, b) => b.tsMs - a.tsMs);
  return out.slice(0, limit);
}

// ---- Friendly timestamps --------------------------------------------------

/**
 * Render a timestamp in the dashboard's three friendliness bands.
 *
 *   - same day → HH:MM
 *   - yesterday → "Yesterday HH:MM"
 *   - older → "DD Mon HH:MM"
 *
 * `nowMs` is injected so the function is testable without a real
 * clock. Defaults to Date.now().
 */
export function friendlyTimestamp(tsMs: number, nowMs: number = Date.now()): string {
  const ts = new Date(tsMs);
  const now = new Date(nowMs);
  const sameYMD =
    ts.getUTCFullYear() === now.getUTCFullYear() &&
    ts.getUTCMonth() === now.getUTCMonth() &&
    ts.getUTCDate() === now.getUTCDate();
  if (sameYMD) {
    return ts.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "UTC",
    });
  }
  const yesterday = new Date(nowMs - 86_400_000);
  const isYesterday =
    ts.getUTCFullYear() === yesterday.getUTCFullYear() &&
    ts.getUTCMonth() === yesterday.getUTCMonth() &&
    ts.getUTCDate() === yesterday.getUTCDate();
  if (isYesterday) {
    return `Yesterday ${ts.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: "UTC",
    })}`;
  }
  return ts.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "UTC",
  });
}
