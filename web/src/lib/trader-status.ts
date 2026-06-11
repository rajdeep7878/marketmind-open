/**
 * Pure classifiers for the four status-strip indicators.
 *
 * Each takes a small input record + `nowMs` (injected so tests are
 * clock-free) and returns one of "green" | "yellow" | "red" |
 * "neutral". The dashboard renders a coloured dot from the result.
 *
 * Tone mapping in the design system:
 *   green   → "positive"  (forest)
 *   yellow  → "accent"    (sienna light / amber dark — the warm
 *                          "warning" channel we already have)
 *   red     → "negative"  (oxblood)
 *   neutral → "muted"
 *
 * Adding a fourth status semantic would require a design-token ADR;
 * the locked palette keeps us to these four.
 */

export type StatusLevel = "green" | "yellow" | "red" | "neutral";

const BOT_FRESH_THRESHOLD_MS = 90 * 1000;
const BOT_STALE_THRESHOLD_MS = 5 * 60 * 1000;

/**
 * Bot status classification.
 *
 * Inputs are the latest_run row's last_heartbeat_at AND the latest
 * snapshot_ts. Both come from /trader/health. The function returns
 * the WORST of the two — because heartbeat-fresh + snapshot-stale
 * means the snapshot phase is broken even if the runner is alive.
 *
 * The runner's heartbeat is written by every phase, but each
 * phase has its own commit point. So a stale heartbeat = nothing
 * has finished a phase in N seconds. Stale snapshot = the
 * snapshot phase specifically hasn't run.
 *
 * If `runStatus` is `"crashed"`, the result is unconditionally red.
 * If the run row is missing (`null`), the result is neutral —
 * the bot has never been started, distinct from "the bot died".
 */
export interface BotStatusInputs {
  runStatus: "running" | "stopped" | "crashed" | null;
  lastHeartbeatMs: number | null;
  lastSnapshotMs: number | null;
  nowMs: number;
}

export function classifyBotStatus(inputs: BotStatusInputs): StatusLevel {
  const { runStatus, lastHeartbeatMs, lastSnapshotMs, nowMs } = inputs;
  if (runStatus === null) return "neutral";
  if (runStatus === "crashed") return "red";
  if (runStatus === "stopped") return "neutral";

  // running — check both freshness signals; worst wins.
  const heartbeatLevel = classifyFreshness(
    lastHeartbeatMs,
    nowMs,
    BOT_FRESH_THRESHOLD_MS,
    BOT_STALE_THRESHOLD_MS,
  );
  // Snapshot lags heartbeat by at most one cycle (~60s). Apply the
  // same thresholds — a snapshot older than 5 min while heartbeat is
  // fresh is a real bug worth flagging.
  const snapshotLevel = classifyFreshness(
    lastSnapshotMs,
    nowMs,
    BOT_FRESH_THRESHOLD_MS,
    BOT_STALE_THRESHOLD_MS,
  );
  return worstOf([heartbeatLevel, snapshotLevel]);
}

/**
 * Market-data freshness. Input is the staleness of the WORST pair
 * (max age across configured symbols/timeframes), in ms, plus the
 * configured TRADER_DATA_STALENESS_SECONDS as the freshness
 * threshold.
 *
 *   age <= threshold       → green
 *   1× < age <= 2× threshold → yellow
 *   age > 2×               → red
 */
export interface MarketDataInputs {
  worstPairAgeMs: number | null;
  staleThresholdMs: number;
}

export function classifyMarketData(inputs: MarketDataInputs): StatusLevel {
  const { worstPairAgeMs, staleThresholdMs } = inputs;
  if (worstPairAgeMs === null) return "neutral";
  if (worstPairAgeMs <= staleThresholdMs) return "green";
  if (worstPairAgeMs <= 2 * staleThresholdMs) return "yellow";
  return "red";
}

/**
 * Safety status. Worst severity in the last 24h of risk events:
 *   none / only info → green
 *   any warning      → yellow
 *   any critical     → red
 */
export interface SafetyInputs {
  recentEventSeverities: ReadonlyArray<"info" | "warning" | "critical">;
}

export function classifySafety(inputs: SafetyInputs): StatusLevel {
  const { recentEventSeverities } = inputs;
  if (recentEventSeverities.some((s) => s === "critical")) return "red";
  if (recentEventSeverities.some((s) => s === "warning")) return "yellow";
  return "green";
}

/**
 * Strategies aggregate. Worst drift health across active versions:
 *   all healthy / no drift data → green
 *   any watch                    → yellow
 *   any breach                   → red
 *
 * Versions with `null` drift_health (no drift row yet) are
 * counted as "no signal" — they don't degrade the indicator.
 */
export interface StrategiesInputs {
  driftHealths: ReadonlyArray<"healthy" | "watch" | "breach" | null>;
}

export function classifyStrategies(inputs: StrategiesInputs): StatusLevel {
  const { driftHealths } = inputs;
  if (driftHealths.length === 0) return "neutral";
  if (driftHealths.some((d) => d === "breach")) return "red";
  if (driftHealths.some((d) => d === "watch")) return "yellow";
  return "green";
}

// ---- helpers --------------------------------------------------------------

function classifyFreshness(
  tsMs: number | null,
  nowMs: number,
  freshThresholdMs: number,
  staleThresholdMs: number,
): StatusLevel {
  if (tsMs === null) return "red";
  const age = nowMs - tsMs;
  if (age <= freshThresholdMs) return "green";
  if (age <= staleThresholdMs) return "yellow";
  return "red";
}

const ORDER: Record<StatusLevel, number> = {
  neutral: 0,
  green: 1,
  yellow: 2,
  red: 3,
};

function worstOf(levels: StatusLevel[]): StatusLevel {
  return levels.reduce((acc, next) => (ORDER[next] > ORDER[acc] ? next : acc), "green" as StatusLevel);
}
