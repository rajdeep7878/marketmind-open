/**
 * /strategies/[strategyId] — display one ExtractionResult.
 *
 * Server component. Fetches GET /strategies/{id} on render. Renders
 * the verdict as a colored badge, the report's parts as tables, and
 * the spec as syntax-highlighted (well — pre-tag formatted) JSON. The
 * client-side component handles the verdict-specific styling so this
 * page stays a thin shell.
 */

import { notFound } from "next/navigation";

import { getStrategy } from "@/lib/extraction";
import { StrategyView } from "./view";

interface PageProps {
  params: { strategyId: string };
}

export default async function StrategyPage({ params }: PageProps) {
  let result;
  try {
    result = await getStrategy(params.strategyId);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (message.includes("404")) {
      notFound();
    }
    throw err;
  }

  return <StrategyView result={result} strategyId={params.strategyId} />;
}
