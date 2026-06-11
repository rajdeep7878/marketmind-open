"use client";

import { useState } from "react";

import { StatusDot } from "@/app/trader/components/status-dot";
import { Skeleton } from "@/components/ui/skeleton";
import { traderFetch } from "@/lib/trader-api";
import type {
  PaperPosition,
  PaperPositionList,
  PaperPositionListPaginated,
  StrategyVersionList,
} from "@/lib/trader-api";
import { friendlyTimestamp } from "@/lib/trader-translations";
import { cn } from "@/lib/utils";
import { usePolling } from "@/lib/use-polling";

/**
 * Section 3 — live activity. Two columns:
 *   - Open positions     (per-position cards with PnL accent)
 *   - Recent trades      (last 8 closed positions, click to expand)
 *
 * Each row's strategy_version_id is mapped back to a human-readable
 * strategy name by joining against /trader/strategies — so a third
 * polling source.
 *
 * Marks "Now $..." prices as best-effort: we don't have an
 * intraday tick stream, so the displayed mark is the latest closed
 * candle's close (PortfolioSnapshot snapshots that already). For
 * v1 the displayed mark uses the position's entry price + unrealised_pnl_pct
 * approximation derived from the position size.
 */

export function LiveActivitySection({
  onLastUpdated,
}: {
  onLastUpdated: (_d: Date) => void;
}): React.ReactElement {
  const open = usePolling<PaperPositionList>({
    fetcher: (signal) => traderFetch<PaperPositionList>("/trader/positions/open", signal),
  });
  const closed = usePolling<PaperPositionListPaginated>({
    fetcher: (signal) =>
      traderFetch<PaperPositionListPaginated>(
        "/trader/positions/closed?limit=8&offset=0",
        signal,
      ),
  });
  const strategies = usePolling<StrategyVersionList>({
    fetcher: (signal) => traderFetch<StrategyVersionList>("/trader/strategies", signal),
  });

  if (open.lastUpdatedAt) onLastUpdated(open.lastUpdatedAt);
  if (closed.lastUpdatedAt) onLastUpdated(closed.lastUpdatedAt);

  const versionNames = new Map<string, string>();
  for (const v of strategies.data?.items ?? []) {
    versionNames.set(v.id, prettyStrategy(v.template, v.version));
  }

  return (
    <section className="grid grid-cols-1 gap-10 lg:grid-cols-2">
      <div>
        <h2 className="font-serif text-2xl text-ink">Open positions</h2>
        <div className="mt-6 flex flex-col gap-4">
          {open.isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : open.error ? (
            <p className="rounded-sm border border-hairline p-4 text-sm text-muted">
              Couldn&rsquo;t load — retrying.
            </p>
          ) : open.data && open.data.items.length > 0 ? (
            open.data.items.map((p) => (
              <OpenPositionCard
                key={p.id}
                position={p}
                strategyName={versionNames.get(p.strategy_version_id)}
              />
            ))
          ) : (
            <p className="rounded-sm border border-hairline p-4 text-sm text-muted">
              No open positions right now. The bot is waiting for signals.
            </p>
          )}
        </div>
      </div>

      <div>
        <h2 className="font-serif text-2xl text-ink">Recent trades</h2>
        <div className="mt-6 flex flex-col">
          {closed.isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : closed.error ? (
            <p className="rounded-sm border border-hairline p-4 text-sm text-muted">
              Couldn&rsquo;t load — retrying.
            </p>
          ) : closed.data && closed.data.items.length > 0 ? (
            <ul className="flex flex-col divide-y divide-hairline">
              {closed.data.items.map((p) => (
                <ClosedTradeRow
                  key={p.id}
                  position={p}
                  strategyName={versionNames.get(p.strategy_version_id)}
                />
              ))}
            </ul>
          ) : (
            <p className="rounded-sm border border-hairline p-4 text-sm text-muted">
              No closed trades yet.
            </p>
          )}
        </div>
      </div>
    </section>
  );
}

function OpenPositionCard({
  position,
  strategyName,
}: {
  position: PaperPosition;
  strategyName: string | undefined;
}): React.ReactElement {
  const entry = Number.parseFloat(position.entry_price);
  const stop = Number.parseFloat(position.stop_price);
  const openedMs = Date.parse(position.entry_ts);
  const openedAgo = friendlyTimestamp(openedMs);

  // We don't have a live tick here; the dashboard doesn't proxy
  // exchange prices. Show entry + stop, and note the user can
  // expect MTM in the equity-curve section.

  return (
    <article className="rounded-sm border border-hairline border-l-2 border-l-accent bg-surface p-5">
      <div className="flex items-baseline justify-between gap-4">
        <span className="font-mono text-base font-medium tabular-nums text-ink">
          {position.symbol}
        </span>
        <span className="eyebrow">{strategyName ?? position.strategy_version_id.slice(0, 8)}</span>
      </div>
      <p className="mt-3 font-mono text-sm tabular-nums text-ink">
        Bought at ${entry.toLocaleString("en-US", { maximumFractionDigits: 2 })}
      </p>
      <p className="mt-1 font-mono text-xs tabular-nums text-muted">
        Stop loss at ${stop.toLocaleString("en-US", { maximumFractionDigits: 2 })} · Opened {openedAgo} UTC
      </p>
    </article>
  );
}

function ClosedTradeRow({
  position,
  strategyName,
}: {
  position: PaperPosition;
  strategyName: string | undefined;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  const pnlRaw = position.realised_pnl;
  const pnl = pnlRaw !== null ? Number.parseFloat(pnlRaw) : 0;
  const pnlPct =
    position.realised_pnl_pct !== null ? Number.parseFloat(position.realised_pnl_pct) * 100 : 0;
  const closedAt = position.exit_ts ? friendlyTimestamp(Date.parse(position.exit_ts)) : "—";

  const tone = pnl > 0 ? "positive" : pnl < 0 ? "negative" : "neutral";

  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 py-3 text-left transition-colors hover:bg-fill"
      >
        <StatusDot level={tone === "positive" ? "green" : tone === "negative" ? "red" : "neutral"} size="sm" />
        <span className="text-sm leading-snug text-ink">
          <span className="text-muted">{closedAt} UTC — </span>
          {position.symbol} trade closed for{" "}
          <span className={cn("font-mono tabular-nums", toneClass(tone))}>
            {pnl >= 0 ? "+" : "−"}£{Math.abs(pnl).toFixed(2)}
          </span>{" "}
          <span className="text-muted">({pnlPct.toFixed(2)}%)</span>
        </span>
      </button>
      {open ? (
        <dl className="ml-5 grid grid-cols-2 gap-x-4 gap-y-1 py-3 font-mono text-xs tabular-nums">
          <dt className="text-muted">Strategy</dt>
          <dd className="text-ink">{strategyName ?? position.strategy_version_id.slice(0, 8)}</dd>
          <dt className="text-muted">Entry</dt>
          <dd className="text-ink">${Number.parseFloat(position.entry_price).toLocaleString("en-US")}</dd>
          <dt className="text-muted">Exit</dt>
          <dd className="text-ink">
            {position.exit_price ? `$${Number.parseFloat(position.exit_price).toLocaleString("en-US")}` : "—"}
          </dd>
          <dt className="text-muted">Reason</dt>
          <dd className="text-ink">{position.close_reason ?? "—"}</dd>
        </dl>
      ) : null}
    </li>
  );
}

function toneClass(tone: "positive" | "negative" | "neutral"): string {
  switch (tone) {
    case "positive":
      return "text-positive";
    case "negative":
      return "text-negative";
    case "neutral":
      return "text-muted";
  }
}

function prettyStrategy(template: string, version: number): string {
  const labels: Record<string, string> = {
    ma_trend: "EMA Trend",
    breakout: "Breakout",
    rsi_mean_reversion: "RSI Mean Reversion",
    bb_mean_reversion: "Bollinger Mean Reversion",
    vcb: "Volatility Compression",
  };
  const base = labels[template] ?? template;
  return `${base} v${version}`;
}
