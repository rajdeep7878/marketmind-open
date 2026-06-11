"use client";

import { StatusDot } from "@/app/trader/components/status-dot";
import { Card } from "@/components/ui/card";
import { traderFetch } from "@/lib/trader-api";
import type {
  HealthResponse,
  RiskStatusResponse,
  StrategyVersionList,
} from "@/lib/trader-api";
import {
  classifyBotStatus,
  classifyMarketData,
  classifySafety,
  classifyStrategies,
  type StatusLevel,
} from "@/lib/trader-status";
import { usePolling } from "@/lib/use-polling";

/**
 * Section 1 — top status strip.
 *
 * Four indicators sharing one polling cycle (30s, set by usePolling
 * default). Each fetches from its own endpoint; we tolerate any of
 * them failing independently — that indicator just shows neutral.
 *
 * Bot status uses BOTH the heartbeat freshness from /trader/health
 * AND the snapshot freshness, so the dashboard catches the case
 * where the snapshot phase specifically has stopped writing even
 * though the runner is alive.
 */

export function StatusStripSection({
  onLastUpdated,
}: {
  onLastUpdated: (_d: Date) => void;
}): React.ReactElement {
  const health = usePolling<HealthResponse>({
    fetcher: (signal) => traderFetch<HealthResponse>("/trader/health", signal),
  });
  const risk = usePolling<RiskStatusResponse>({
    fetcher: (signal) => traderFetch<RiskStatusResponse>("/trader/risk/status", signal),
  });
  const strategies = usePolling<StrategyVersionList>({
    fetcher: (signal) => traderFetch<StrategyVersionList>("/trader/strategies", signal),
  });

  // Surface the most-recent successful refresh across the strip
  // to the page-level "Last updated" indicator.
  const latest = mostRecent([
    health.lastUpdatedAt,
    risk.lastUpdatedAt,
    strategies.lastUpdatedAt,
  ]);
  if (latest !== null) onLastUpdated(latest);

  const nowMs = health.data ? Date.parse(health.data.now) : Date.now();

  const botLevel: StatusLevel = health.error
    ? "neutral"
    : classifyBotStatus({
        runStatus: health.data?.latest_run?.status ?? null,
        lastHeartbeatMs: health.data?.latest_run
          ? Date.parse(health.data.latest_run.last_heartbeat_at)
          : null,
        lastSnapshotMs: health.data?.last_snapshot_ts
          ? Date.parse(health.data.last_snapshot_ts)
          : null,
        nowMs,
      });

  // Market data freshness: derive from /trader/health.last_snapshot_ts
  // as a proxy for "the ingest phase ran recently" — the ingest phase
  // runs every cycle and would surface the freshness more accurately
  // via a dedicated endpoint. For v1 the snapshot proxy is fine; a
  // dedicated market-data freshness endpoint is a v1.1 candidate.
  const lastSnapMs = health.data?.last_snapshot_ts
    ? Date.parse(health.data.last_snapshot_ts)
    : null;
  const marketLevel: StatusLevel = health.error
    ? "neutral"
    : classifyMarketData({
        worstPairAgeMs: lastSnapMs !== null ? nowMs - lastSnapMs : null,
        // Match TraderSettings.trader_data_staleness_seconds default.
        staleThresholdMs: 600 * 1000,
      });

  const safetyLevel: StatusLevel = risk.error
    ? "neutral"
    : classifySafety({
        recentEventSeverities: (risk.data?.recent_risk_events ?? []).map(
          (e) => e.severity,
        ),
      });

  const stratLevel: StatusLevel = strategies.error
    ? "neutral"
    : classifyStrategies({
        driftHealths: (strategies.data?.items ?? [])
          .filter((s) => s.enabled && s.approved_for_paper)
          .map((s) => s.latest_drift_health),
      });

  return (
    <section aria-label="Bot status indicators" className="grid grid-cols-1 gap-px bg-hairline sm:grid-cols-2 lg:grid-cols-4">
      <Indicator
        label="Bot"
        level={botLevel}
        detail={botDetail(health.data, health.error !== null)}
      />
      <Indicator
        label="Market data"
        level={marketLevel}
        detail={marketDetail(lastSnapMs, nowMs, health.error !== null)}
      />
      <Indicator
        label="Safety"
        level={safetyLevel}
        detail={safetyDetail(risk.data, risk.error !== null)}
      />
      <Indicator
        label="Strategies"
        level={stratLevel}
        detail={strategiesDetail(strategies.data, strategies.error !== null)}
      />
    </section>
  );
}

function Indicator({
  label,
  level,
  detail,
}: {
  label: string;
  level: StatusLevel;
  detail: string;
}): React.ReactElement {
  return (
    <Card surface className="rounded-none border-0 p-5" title={detail}>
      <div className="flex items-center gap-2">
        <StatusDot level={level} aria-label={`${label}: ${levelText(level)}`} />
        <span className="eyebrow">{label}</span>
      </div>
      <p className="mt-2 text-sm leading-snug text-ink">{levelText(level)}</p>
      <p className="mt-1 text-xs leading-snug text-muted">{detail}</p>
    </Card>
  );
}

function levelText(level: StatusLevel): string {
  switch (level) {
    case "green":
      return "All good";
    case "yellow":
      return "Worth watching";
    case "red":
      return "Needs attention";
    case "neutral":
      return "No data yet";
  }
}

function botDetail(health: HealthResponse | null, errored: boolean): string {
  if (errored) return "Couldn't load — retrying";
  if (!health) return "Loading…";
  if (!health.latest_run) return "Bot has never been started.";
  const status = health.latest_run.status;
  if (status === "crashed") return "Latest run crashed. Restart the runner.";
  if (status === "stopped") return "Latest run shut down cleanly.";
  if (!health.last_snapshot_ts) return "Running — no snapshot written yet.";
  const lastHb = Date.parse(health.latest_run.last_heartbeat_at);
  const nowMs = Date.parse(health.now);
  const sec = Math.max(0, Math.round((nowMs - lastHb) / 1000));
  return `Running — heartbeat ${sec}s ago.`;
}

function marketDetail(snapMs: number | null, nowMs: number, errored: boolean): string {
  if (errored) return "Couldn't load — retrying";
  if (snapMs === null) return "No market data yet.";
  const sec = Math.max(0, Math.round((nowMs - snapMs) / 1000));
  return `Latest cycle ${sec}s ago.`;
}

function safetyDetail(risk: RiskStatusResponse | null, errored: boolean): string {
  if (errored) return "Couldn't load — retrying";
  if (!risk) return "Loading…";
  if (risk.kill_switch_tripped) return "Kill switch tripped.";
  const evs = risk.recent_risk_events;
  if (evs.length === 0) return "No risk events in recent window.";
  const crit = evs.filter((e) => e.severity === "critical").length;
  const warn = evs.filter((e) => e.severity === "warning").length;
  if (crit > 0) return `${crit} critical event(s) recently.`;
  if (warn > 0) return `${warn} warning event(s) recently.`;
  return "Only info events recently.";
}

function strategiesDetail(
  strats: StrategyVersionList | null,
  errored: boolean,
): string {
  if (errored) return "Couldn't load — retrying";
  if (!strats) return "Loading…";
  const active = strats.items.filter((s) => s.enabled && s.approved_for_paper);
  if (active.length === 0) return "No active strategies.";
  const breach = active.filter((s) => s.latest_drift_health === "breach").length;
  const watch = active.filter((s) => s.latest_drift_health === "watch").length;
  if (breach > 0) return `${breach} strategy in breach.`;
  if (watch > 0) return `${watch} on watch.`;
  return `${active.length} active, all healthy.`;
}

function mostRecent(dates: ReadonlyArray<Date | null>): Date | null {
  const valid = dates.filter((d): d is Date => d !== null);
  if (valid.length === 0) return null;
  return valid.reduce((acc, d) => (d.getTime() > acc.getTime() ? d : acc));
}
