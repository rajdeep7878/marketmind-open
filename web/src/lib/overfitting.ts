/**
 * Phase 4 overfitting-analysis types + API helpers.
 *
 * Mirrors `shared/.../schemas/overfitting.py`. Kept hand-rolled vs.
 * the generated TS bundle so the frontend stays buildable without
 * running `gen:types`.
 */

import { apiBaseUrl } from "@/lib/env";

// ---- Walk-forward --------------------------------------------------------

export interface WindowResult {
  window_index: number;
  in_sample_start: string;
  in_sample_end: string;
  out_of_sample_start: string;
  out_of_sample_end: string;
  in_sample_return_pct: number;
  in_sample_sharpe: number;
  in_sample_num_trades: number;
  out_of_sample_return_pct: number;
  out_of_sample_sharpe: number;
  out_of_sample_num_trades: number;
}

export interface WalkForwardResult {
  windows: WindowResult[];
  in_sample_avg_return: number;
  out_of_sample_avg_return: number;
  degradation_ratio: number;
  degradation_ratio_valid: boolean;
  out_of_sample_positive_rate: number;
  consistency_score: number;
  train_ratio: number;
  n_windows_requested: number;
  n_windows_actual: number;
}

// ---- Parameter sweep ----------------------------------------------------

export type SweepAxisKind =
  | "stop_loss_pct"
  | "take_profit_pct"
  | "indicator_period"
  | "rsi_lower_threshold"
  | "rsi_upper_threshold";

export interface SweepAxis {
  kind: SweepAxisKind;
  label: string;
  values: number[];
  baseline_value: number;
  target_paths: string[];
}

export interface SweepCell {
  axis_values: Record<string, number>;
  total_return_pct: number;
  sharpe_ratio: number;
  num_trades: number;
  is_baseline: boolean;
}

export interface ParameterSweepResult {
  axes: SweepAxis[];
  cells: SweepCell[];
  baseline_return_pct: number;
  baseline_rank_percentile: number;
  best_in_grid_return: number;
  worst_in_grid_return: number;
  neighborhood_avg_return: number;
  peakiness_score: number;
  n_combinations: number;
  skipped_reason: string | null;
}

// ---- Monte Carlo --------------------------------------------------------

export interface MonteCarloHistogramBin {
  lo: number;
  hi: number;
  count: number;
}

export interface MonteCarloResult {
  real_return_pct: number;
  real_sharpe: number;
  n_permutations: number;
  synthetic_mean_return: number;
  synthetic_std_return: number;
  synthetic_min: number;
  synthetic_max: number;
  histogram: MonteCarloHistogramBin[];
  p_value: number;
  percentile_rank: number;
  seed: number;
}

// ---- Deflated Sharpe ----------------------------------------------------

export interface DeflatedSharpeResult {
  observed_sharpe: number;
  deflated_sharpe_ratio: number;
  probability_strategy_is_real: number;
  n_trials_estimate: number;
  n_observations: number;
  returns_skewness: number;
  returns_kurtosis: number;
  expected_max_sharpe: number;
  method: "lopez_de_prado_full" | "haircut_v1";
}

// ---- Composite ----------------------------------------------------------

export type OverfittingVerdict = "likely_robust" | "mixed_signals" | "likely_overfit";

export interface SignalContribution {
  name: "walk_forward" | "parameter_sweep" | "monte_carlo" | "deflated_sharpe";
  label: string;
  raw_value: number;
  weight: number;
  contribution_pts: number;
}

export interface OverfittingScore {
  score: number;
  verdict: OverfittingVerdict;
  contributions: SignalContribution[];
  explanation: string;
  confidence_band_low: number;
  confidence_band_high: number;
}

// ---- Top-level ----------------------------------------------------------

export interface OverfittingAnalysis {
  schema_version: "1.0";
  walk_forward: WalkForwardResult;
  parameter_sweep: ParameterSweepResult;
  monte_carlo: MonteCarloResult;
  deflated_sharpe: DeflatedSharpeResult;
  composite: OverfittingScore;
  compute_seconds: number;
}

export interface OverfittingStartedResponse {
  job_id: string;
  from_cache: boolean;
  analysis_id: string | null;
}

export interface BacktestOverfittingResponse {
  analysis_id: string;
  analysis: OverfittingAnalysis;
}

export interface JobProgress {
  job_id: string;
  status: "queued" | "started" | "finished" | "failed" | "deferred";
  step: string | null;
  current: number | null;
  total: number | null;
}

// ---- API surface --------------------------------------------------------

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
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

export async function postOverfitting(backtestId: string): Promise<OverfittingStartedResponse> {
  return jsonFetch<OverfittingStartedResponse>(`/backtests/${backtestId}/overfitting`, {
    method: "POST",
  });
}

export async function getOverfittingForBacktest(
  backtestId: string,
): Promise<BacktestOverfittingResponse | null> {
  const res = await fetch(`${apiBaseUrl()}/backtests/${backtestId}/overfitting`, {
    cache: "no-store",
  });
  if (res.status === 404) {
    return null;
  }
  if (!res.ok) {
    throw new Error(`GET /backtests/${backtestId}/overfitting -> ${res.status}`);
  }
  return (await res.json()) as BacktestOverfittingResponse;
}

export async function getOverfitting(analysisId: string): Promise<OverfittingAnalysis> {
  return jsonFetch<OverfittingAnalysis>(`/overfitting/${analysisId}`);
}

export async function getJobProgress(jobId: string): Promise<JobProgress> {
  return jsonFetch<JobProgress>(`/jobs/${jobId}/progress`);
}
