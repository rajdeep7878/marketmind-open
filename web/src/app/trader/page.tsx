"use client";

import Link from "next/link";
import { useCallback, useState } from "react";

import { LastUpdated } from "@/app/trader/components/last-updated";
import { ActivityLogSection } from "@/app/trader/sections/activity-log";
import { LiveActivitySection } from "@/app/trader/sections/live-activity";
import { MoneySection } from "@/app/trader/sections/money";
import { StatusStripSection } from "@/app/trader/sections/status-strip";
import { StrategiesSection } from "@/app/trader/sections/strategies";
import { Breadcrumbs } from "@/components/nav/breadcrumbs";
import { Separator } from "@/components/ui/separator";
import { useRecordRecentPage } from "@/lib/use-record-recent-page";

/**
 * /trader — the operator dashboard.
 *
 * Top-level client component. Each section owns its own polling
 * hook (30s default) and reports its most-recent successful
 * refresh time up to this page so the "Last updated" indicator
 * in the corner aggregates them.
 *
 * Page composition (top to bottom):
 *   1. Status strip — four colour-dot indicators
 *   2. Money — balance + today + all-time + equity curve
 *   3. Live activity — open positions + recent trades
 *   4. Strategies — grid with pause/resume
 *   5. Activity log — merged audit + signals + alerts feed
 *
 * The page is desktop-first; sections drop to single-column on
 * narrow viewports only via the default Tailwind responsive
 * utilities, not custom mobile work.
 */

export default function TraderPage(): React.ReactElement {
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const reportUpdated = useCallback((_d: Date): void => {
    setLastUpdated((prev) => (prev === null || _d.getTime() > prev.getTime() ? _d : prev));
  }, []);

  useRecordRecentPage({ path: "/trader", title: "Trader dashboard" });

  return (
    <main className="mx-auto w-full max-w-[1200px] px-6 py-10 sm:px-10">
      <PageHeader lastUpdated={lastUpdated} />

      <div className="mt-10">
        <StatusStripSection onLastUpdated={reportUpdated} />
      </div>

      <Separator className="my-14" />
      <MoneySection onLastUpdated={reportUpdated} />

      <Separator className="my-14" />
      <LiveActivitySection onLastUpdated={reportUpdated} />

      <Separator className="my-14" />
      <StrategiesSection onLastUpdated={reportUpdated} />

      <Separator className="my-14" />
      <ActivityLogSection onLastUpdated={reportUpdated} />

      <PageFooter />
    </main>
  );
}

function PageHeader({ lastUpdated }: { lastUpdated: Date | null }): React.ReactElement {
  // The chrome (theme toggle + ⌘K trigger) lives in the global
  // NavRail mounted by the root layout. We keep LastUpdated here
  // because it's specifically a Trader-page concern.
  return (
    <header className="flex items-end justify-between border-b border-hairline pb-4">
      <Breadcrumbs items={[{ label: "Trader" }]} />
      <LastUpdated ts={lastUpdated} />
    </header>
  );
}

function PageFooter(): React.ReactElement {
  return (
    <footer className="mt-16 border-t border-hairline pt-6 text-xs leading-snug text-muted">
      <p>
        Paper-only trader. Read-only dashboard. Admin actions require the same{" "}
        <span className="font-mono tabular-nums">ADMIN_USERNAME</span> /{" "}
        <span className="font-mono tabular-nums">ADMIN_PASSWORD</span> as the API. See{" "}
        <Link href="/" className="text-ink underline decoration-muted hover:decoration-ink">
          MarketMind home
        </Link>
        .
      </p>
    </footer>
  );
}
