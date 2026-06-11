"use client";

import { EquityCurveChart } from "@/app/trader/components/equity-curve-chart";
import { Skeleton } from "@/components/ui/skeleton";
import { traderFetch } from "@/lib/trader-api";
import type { EquityCurveResponse, PortfolioSnapshot } from "@/lib/trader-api";
import {
  computeTodayDelta,
  interpretEquityPerformance,
} from "@/lib/trader-interpretation";
import { cn } from "@/lib/utils";
import { usePolling } from "@/lib/use-polling";

/**
 * Section 2 — money. Hero balance + today + all-time + equity curve
 * + plain-English interpretation.
 *
 * Two polled endpoints:
 *   /trader/portfolio/current       — headline number
 *   /trader/portfolio/equity_curve  — the chart + today/start-of-day
 *
 * The starting cash is taken from the FIRST point on the equity
 * curve. That sidesteps the need to read it from
 * TRADER_STARTING_CASH_GBP (the env var) — the bot itself wrote the
 * baseline.
 */

const STARTING_CASH_FALLBACK_GBP = 1000;

export function MoneySection({
  onLastUpdated,
}: {
  onLastUpdated: (_d: Date) => void;
}): React.ReactElement {
  const current = usePolling<PortfolioSnapshot | null>({
    fetcher: (signal) => traderFetch<PortfolioSnapshot | null>("/trader/portfolio/current", signal),
  });
  const curve = usePolling<EquityCurveResponse>({
    fetcher: (signal) => traderFetch<EquityCurveResponse>("/trader/portfolio/equity_curve", signal),
  });

  if (current.lastUpdatedAt) onLastUpdated(current.lastUpdatedAt);
  if (curve.lastUpdatedAt) onLastUpdated(curve.lastUpdatedAt);

  if (current.isLoading || curve.isLoading) {
    return (
      <section className="grid grid-cols-1 gap-8 lg:grid-cols-12">
        <div className="lg:col-span-7">
          <Skeleton className="h-14 w-72" />
          <Skeleton className="mt-4 h-4 w-48" />
        </div>
        <div className="lg:col-span-5 lg:text-right">
          <Skeleton className="h-4 w-32 lg:ml-auto" />
          <Skeleton className="mt-3 h-8 w-48 lg:ml-auto" />
        </div>
        <div className="lg:col-span-12">
          <Skeleton className="h-72 w-full" />
        </div>
      </section>
    );
  }

  if (current.error || curve.error) {
    return (
      <section className="rounded-sm border border-hairline p-6 text-sm text-muted">
        Couldn&rsquo;t load portfolio data — retrying.
      </section>
    );
  }

  const points = curve.data?.points ?? [];
  const firstPoint = points.at(0);
  const startingCash = firstPoint
    ? Number.parseFloat(firstPoint.equity)
    : STARTING_CASH_FALLBACK_GBP;

  const currentEquity = current.data ? Number.parseFloat(current.data.equity) : startingCash;

  // Days elapsed: first equity point to now.
  const firstTsMs = firstPoint ? Date.parse(firstPoint.ts) : Date.now();
  const daysElapsed = Math.max(0, (Date.now() - firstTsMs) / 86_400_000);

  // Start-of-today equity: the LATEST point on or before
  // 00:00 UTC today.
  const startOfDayMs = (() => {
    const d = new Date();
    d.setUTCHours(0, 0, 0, 0);
    return d.getTime();
  })();
  const startOfDayPoint = [...points]
    .reverse()
    .find((p) => Date.parse(p.ts) <= startOfDayMs);
  const startOfDayEquity = startOfDayPoint
    ? Number.parseFloat(startOfDayPoint.equity)
    : null;

  const todayDelta = computeTodayDelta(currentEquity, startOfDayEquity);
  const interpretation = interpretEquityPerformance({
    currentEquity,
    startingCash,
    daysElapsed,
  });

  const allTimeDelta = currentEquity - startingCash;
  const allTimeTone =
    allTimeDelta > 0 ? "positive" : allTimeDelta < 0 ? "negative" : "neutral";

  return (
    <section className="grid grid-cols-1 gap-8 lg:grid-cols-12">
      <div className="lg:col-span-7">
        <p className="eyebrow">Balance</p>
        <p className="mt-2 font-mono text-4xl tabular-nums text-ink">
          {formatGbp(currentEquity)}
        </p>
        <p className={cn("mt-3 font-mono text-sm tabular-nums", deltaToneClass(todayDelta?.tone ?? "neutral"))}>
          {todayDelta
            ? `${signOf(todayDelta.amount)}${formatGbpAbs(todayDelta.amount)} (${signOf(todayDelta.pct)}${todayDelta.pct.toFixed(2)}%) today`
            : "Today — no comparison snapshot yet."}
        </p>
      </div>
      <div className="lg:col-span-5 lg:text-right">
        <p className="eyebrow">All-time</p>
        <p className={cn("mt-2 font-mono text-2xl tabular-nums", deltaToneClass(allTimeTone))}>
          {signOf(allTimeDelta)}{formatGbpAbs(allTimeDelta)}
        </p>
        <p className="mt-1 text-xs text-muted">
          since you started {Math.max(1, Math.round(daysElapsed))} {Math.round(daysElapsed) === 1 ? "day" : "days"} ago
        </p>
      </div>
      <div className="lg:col-span-12">
        {points.length > 1 ? (
          <EquityCurveChart points={points} startingCash={startingCash} />
        ) : (
          <p className="rounded-sm border border-hairline p-6 text-sm text-muted">
            Not enough data points yet to draw an equity curve.
          </p>
        )}
        <p className="mt-4 max-w-prose font-serif text-base leading-relaxed text-muted">
          {interpretation}
        </p>
      </div>
    </section>
  );
}

function formatGbp(n: number): string {
  return `£${n.toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatGbpAbs(n: number): string {
  return `£${Math.abs(n).toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function signOf(n: number): string {
  if (n > 0) return "+";
  if (n < 0) return "−"; // U+2212 minus sign, not hyphen
  return "";
}

function deltaToneClass(tone: "positive" | "negative" | "neutral"): string {
  switch (tone) {
    case "positive":
      return "text-positive";
    case "negative":
      return "text-negative";
    case "neutral":
      return "text-muted";
  }
}
