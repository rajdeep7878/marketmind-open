/**
 * Client-side types + helpers for the Phase 2.2 extraction flow.
 *
 * We deliberately don't import types from `src/types/generated/schemas.ts`
 * here because that file is regenerated locally and isn't committed.
 * Hand-rolled minimal versions live below — keep them in lockstep
 * with the Pydantic models in `shared/src/marketmind_shared/schemas/`.
 */

import { apiBaseUrl } from "@/lib/env";

// ---- response shapes ------------------------------------------------------

export type ExtractionVerdict =
  | "fully_extractable"
  | "partially_extractable"
  | "not_extractable"
  | "not_a_strategy";

export interface ExtractedRule {
  field: string;
  value_description: string;
  extractable: boolean;
  confidence: number;
  quote: string | null;
}

export interface AuthorClaim {
  claim_type: "return" | "drawdown" | "win_rate" | "trade_count" | "sharpe" | "other";
  value: string;
  timeframe: string | null;
  instrument: string | null;
  period: string | null;
  quote: string;
}

export interface ExtractionReport {
  verdict: ExtractionVerdict;
  overall_confidence: number;
  summary: string;
  extracted_rules: ExtractedRule[];
  backtestable_parts: string[];
  non_backtestable_parts: string[];
  author_claims: AuthorClaim[];
  reasoning: string;
  refusal_explanation: string | null;
}

export interface ExtractionResult {
  spec: Record<string, unknown> | null;
  report: ExtractionReport;
}

export interface JobView {
  id: string;
  kind: string;
  status: "queued" | "started" | "finished" | "failed" | "deferred";
  result: Record<string, unknown> | null;
  error: string | null;
}

export interface ExtractionStartedResponse {
  job_id: string;
  from_cache: boolean;
  extraction_id: string | null;
}

// ---- API surface ----------------------------------------------------------

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // Resolve at call time, not module-load time: the same module is
  // executed in both the browser bundle and the Node SSR runtime, and
  // they need different base URLs (see lib/env.ts apiBaseUrl).
  const res = await fetch(`${apiBaseUrl()}${path}`, {
    cache: "no-store",
    ...(init ?? {}),
    headers: {
      "content-type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${init?.method ?? "GET"} ${path} -> ${res.status}: ${body}`);
  }
  return (await res.json()) as T;
}

export async function postIngest(url: string): Promise<JobView> {
  return jsonFetch<JobView>("/content/ingest", {
    method: "POST",
    body: JSON.stringify({ url }),
  });
}

export async function postTranscribe(contentId: string): Promise<JobView> {
  return jsonFetch<JobView>(`/content/${contentId}/transcribe`, { method: "POST" });
}

export async function postExtract(contentId: string): Promise<ExtractionStartedResponse> {
  return jsonFetch<ExtractionStartedResponse>(`/content/${contentId}/extract`, {
    method: "POST",
  });
}

export async function getJob(jobId: string): Promise<JobView> {
  return jsonFetch<JobView>(`/jobs/${jobId}`);
}

export async function getStrategy(strategyId: string): Promise<ExtractionResult> {
  return jsonFetch<ExtractionResult>(`/strategies/${strategyId}`);
}

// ---- polling helper -------------------------------------------------------

/**
 * Poll GET /jobs/{id} until status is finished/failed.
 *
 * Returns the final JobView. Caller is responsible for interpreting
 * result vs error.
 *
 * Polls every 1s up to 5 minutes; throws if the wall-clock budget is
 * exceeded. We deliberately don't use exponential backoff here — the
 * jobs we wait on (ingest_youtube, transcribe, extract_strategy) all
 * finish in 1-60 seconds, so a flat 1s cadence keeps the UI feeling
 * snappy without hammering the API.
 */
export async function pollJob(
  jobId: string,
  options?: { timeoutMs?: number; intervalMs?: number; signal?: AbortSignal },
): Promise<JobView> {
  const timeoutMs = options?.timeoutMs ?? 5 * 60 * 1000;
  const intervalMs = options?.intervalMs ?? 1000;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (options?.signal?.aborted) {
      throw new Error("polling aborted");
    }
    const job = await getJob(jobId);
    if (job.status === "finished" || job.status === "failed") {
      return job;
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error(`polling for job ${jobId} timed out after ${timeoutMs}ms`);
}
