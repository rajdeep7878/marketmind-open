/**
 * /backtests/[backtestId] — show one Phase 3.2 backtest run.
 *
 * Server component. Fetches GET /backtests/{id} on render (using the
 * server-internal API URL when running in compose). The equity curve
 * comes back already downsampled to ≤ 500 points so the client-side
 * chart renders fast.
 */

import { notFound } from "next/navigation";

import { getBacktest } from "@/lib/backtest";
import { getOverfittingForBacktest } from "@/lib/overfitting";

import { BacktestView } from "./view";

interface PageProps {
  params: { backtestId: string };
}

export default async function BacktestPage({ params }: PageProps) {
  let result;
  try {
    result = await getBacktest(params.backtestId);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (message.includes("404")) {
      notFound();
    }
    throw err;
  }

  // Best-effort: load any existing overfitting analysis so the section
  // renders pre-hydrated. A 404 there just means we haven't run one
  // yet — the section shows the "Run analysis" button in that case.
  let initialAnalysis = null;
  try {
    const wrapper = await getOverfittingForBacktest(params.backtestId);
    initialAnalysis = wrapper?.analysis ?? null;
  } catch {
    // Swallow — UI falls back to the no-analysis state.
  }

  return (
    <BacktestView
      result={result}
      backtestId={params.backtestId}
      initialOverfittingAnalysis={initialAnalysis}
    />
  );
}
