"use client";

import { useCallback, useState } from "react";

import { AdminCredentialsModal } from "@/app/trader/components/admin-credentials-modal";
import { StatusDot } from "@/app/trader/components/status-dot";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardEyebrow, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ADMIN_AUTH_DISABLED,
  ADMIN_AUTH_INVALID,
  ADMIN_AUTH_REQUIRED,
  disableStrategy,
  enableStrategy,
  hasAdminCreds,
} from "@/lib/trader-admin";
import { traderFetch } from "@/lib/trader-api";
import type {
  DriftList,
  DriftMetric,
  PaperPositionList,
  StrategyVersionList,
  StrategyVersionSummary,
} from "@/lib/trader-api";
import { translateDriftStatus } from "@/lib/trader-translations";
import { cn } from "@/lib/utils";
import { usePolling } from "@/lib/use-polling";

/**
 * Section 4 — strategies grid.
 *
 * Joins three sources:
 *   /trader/strategies          → version list + drift status
 *   /trader/drift/latest        → fuller drift metrics (vs. backtest)
 *   /trader/positions/open      → for "trades this version is in"
 *
 * Pause / Resume button drives the admin modal flow. On success the
 * polling automatically refreshes; we don't optimistically mutate.
 */

export function StrategiesSection({
  onLastUpdated,
}: {
  onLastUpdated: (_d: Date) => void;
}): React.ReactElement {
  const strategies = usePolling<StrategyVersionList>({
    fetcher: (signal) => traderFetch<StrategyVersionList>("/trader/strategies", signal),
  });
  const drift = usePolling<DriftList>({
    fetcher: (signal) => traderFetch<DriftList>("/trader/drift/latest", signal),
  });
  const open = usePolling<PaperPositionList>({
    fetcher: (signal) => traderFetch<PaperPositionList>("/trader/positions/open", signal),
  });

  if (strategies.lastUpdatedAt) onLastUpdated(strategies.lastUpdatedAt);

  const [pendingAction, setPendingAction] = useState<
    { kind: "disable" | "enable"; versionId: string } | null
  >(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const tryAction = useCallback(
    async (action: { kind: "disable" | "enable"; versionId: string }): Promise<void> => {
      setActionError(null);
      try {
        if (action.kind === "disable") await disableStrategy(action.versionId);
        else await enableStrategy(action.versionId);
        strategies.refresh();
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        if (msg === ADMIN_AUTH_REQUIRED || msg === ADMIN_AUTH_INVALID) {
          // Show modal — pendingAction stays set so onSuccess can retry.
          setPendingAction(action);
        } else if (msg === ADMIN_AUTH_DISABLED) {
          setActionError("Admin auth is disabled on the server.");
        } else {
          setActionError(`Action failed: ${msg}`);
        }
      }
    },
    [strategies],
  );

  const handleClick = useCallback(
    (versionId: string, currentlyEnabled: boolean): void => {
      const action = {
        kind: currentlyEnabled ? "disable" : "enable",
        versionId,
      } as const;
      if (!hasAdminCreds()) {
        setPendingAction(action);
        return;
      }
      void tryAction(action);
    },
    [tryAction],
  );

  const handleModalSuccess = useCallback((): void => {
    const action = pendingAction;
    setPendingAction(null);
    if (action) void tryAction(action);
  }, [pendingAction, tryAction]);

  const handleModalCancel = useCallback((): void => {
    setPendingAction(null);
  }, []);

  if (strategies.isLoading) {
    return (
      <section>
        <h2 className="font-serif text-2xl text-ink">Your strategies</h2>
        <div className="mt-6 grid grid-cols-1 gap-6 md:grid-cols-2">
          <Skeleton className="h-56 w-full" />
          <Skeleton className="h-56 w-full" />
        </div>
      </section>
    );
  }

  if (strategies.error) {
    return (
      <section>
        <h2 className="font-serif text-2xl text-ink">Your strategies</h2>
        <p className="mt-4 rounded-sm border border-hairline p-4 text-sm text-muted">
          Couldn&rsquo;t load — retrying.
        </p>
      </section>
    );
  }

  const items = strategies.data?.items ?? [];
  const driftByVersion = new Map<string, DriftMetric>();
  for (const d of drift.data?.items ?? []) {
    driftByVersion.set(d.strategy_version_id, d);
  }
  const openPositionsByVersion = new Map<string, number>();
  for (const p of open.data?.items ?? []) {
    openPositionsByVersion.set(
      p.strategy_version_id,
      (openPositionsByVersion.get(p.strategy_version_id) ?? 0) + 1,
    );
  }

  return (
    <section>
      <div className="flex items-baseline justify-between">
        <h2 className="font-serif text-2xl text-ink">Your strategies</h2>
        {actionError ? (
          <p className="font-sans text-xs text-negative" role="alert">
            {actionError}
          </p>
        ) : null}
      </div>
      <div className="mt-6 grid grid-cols-1 gap-6 md:grid-cols-2">
        {items.length === 0 ? (
          <p className="rounded-sm border border-hairline p-4 text-sm text-muted md:col-span-2">
            No strategies seeded yet. Use{" "}
            <span className="font-mono text-xs tabular-nums">scripts/trader_seed_strategy.py</span>{" "}
            to add one.
          </p>
        ) : (
          items.map((v) => (
            <StrategyCard
              key={v.id}
              version={v}
              drift={driftByVersion.get(v.id) ?? null}
              openPositions={openPositionsByVersion.get(v.id) ?? 0}
              onToggle={() => handleClick(v.id, v.enabled)}
            />
          ))
        )}
      </div>

      <AdminCredentialsModal
        open={pendingAction !== null}
        onSuccess={handleModalSuccess}
        onCancel={handleModalCancel}
      />
    </section>
  );
}

function StrategyCard({
  version,
  drift,
  openPositions,
  onToggle,
}: {
  version: StrategyVersionSummary;
  drift: DriftMetric | null;
  openPositions: number;
  onToggle: () => void;
}): React.ReactElement {
  const driftLabel = translateDriftStatus(version.latest_drift_health);
  const versionName = `${prettyTemplate(version.template)} v${version.version}`;

  // 2×2 stat grid — pull what we can from the drift row.
  const trades = drift ? drift.paper_trade_count : 0;
  const winRate = drift ? Number.parseFloat(drift.paper_win_rate) : null;
  const avgPerTrade = drift ? Number.parseFloat(drift.paper_avg_return_per_trade) : null;
  // v1.1 will surface aggregate PnL per strategy version once the
  // backend exposes it; for now the 2x2 grid uses the four drift
  // measurements directly.

  const driftAccent =
    driftLabel.tone === "positive"
      ? "border-l-positive"
      : driftLabel.tone === "accent"
        ? "border-l-accent"
        : driftLabel.tone === "negative"
          ? "border-l-negative"
          : "border-l-hairline";

  return (
    <Card className={cn("border-l-2", driftAccent)} surface>
      <div className="flex items-baseline justify-between">
        <CardTitle className="text-lg">{versionName}</CardTitle>
        {!version.approved_for_paper ? (
          <Badge intent="neutral">Not approved</Badge>
        ) : version.enabled ? (
          <Badge intent="positive">Active</Badge>
        ) : (
          <Badge intent="neutral">Paused</Badge>
        )}
      </div>
      <CardEyebrow className="mt-1">{version.symbols.join(", ")} · {version.timeframes.join(", ")}</CardEyebrow>

      <div className="mt-5 flex items-center gap-2" title={driftLabel.explanation}>
        <StatusDot
          level={driftLabel.tone === "positive" ? "green" : driftLabel.tone === "accent" ? "yellow" : driftLabel.tone === "negative" ? "red" : "neutral"}
          aria-label={driftLabel.label}
        />
        <span className="text-sm text-ink">{driftLabel.label}</span>
      </div>

      <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-3 border-y border-hairline py-4">
        <Stat label="Trades">
          <span className="num">{trades}</span>
        </Stat>
        <Stat label="Win rate">
          <span className="num">{winRate !== null ? `${(winRate * 100).toFixed(0)}%` : "—"}</span>
        </Stat>
        <Stat label="Avg / trade">
          <span className="num">
            {avgPerTrade !== null
              ? `${avgPerTrade >= 0 ? "+" : "−"}${Math.abs(avgPerTrade * 100).toFixed(2)}%`
              : "—"}
          </span>
        </Stat>
        <Stat label="Open">
          <span className="num">{openPositions}</span>
        </Stat>
      </dl>

      <div className="mt-5 flex justify-end">
        {version.approved_for_paper ? (
          <Button
            intent="secondary"
            size="sm"
            onClick={onToggle}
          >
            {version.enabled ? "Pause this strategy" : "Resume"}
          </Button>
        ) : (
          <span className="text-xs text-muted">
            Approve via{" "}
            <span className="font-mono tabular-nums">POST /trader/strategies/{version.id.slice(0, 8)}/approve_paper</span>
          </span>
        )}
      </div>

    </Card>
  );
}

function Stat({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <div>
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-1 font-mono text-base tabular-nums text-ink">{children}</dd>
    </div>
  );
}

function prettyTemplate(template: string): string {
  const labels: Record<string, string> = {
    ma_trend: "EMA Trend",
    breakout: "Breakout",
    rsi_mean_reversion: "RSI Mean Reversion",
    bb_mean_reversion: "Bollinger Mean Reversion",
    vcb: "Volatility Compression",
  };
  return labels[template] ?? template;
}
