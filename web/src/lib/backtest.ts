/**
 * Phase 3.2 backtest types + API helpers.
 *
 * Mirrors `shared/.../schemas/backtest_metrics.py` and `backtest_run.py`.
 * Kept hand-rolled (vs. the generated TS bundle) so the FE doesn't
 * depend on running `gen:types` during dev. If the schemas drift, the
 * route tests on the Python side will surface the gap; here we'd see a
 * type mismatch on first use.
 */

import { apiBaseUrl } from "@/lib/env";

// ---- data shapes ----------------------------------------------------------

export interface EquityPoint {
  timestamp: string;
  value: number;
}

export interface BenchmarkEquityPoint {
  timestamp: string;
  value: number;
}

export interface Trade {
  entry_time: string;
  exit_time: string;
  entry_price: number;
  exit_price: number;
  size: number;
  pnl: number;
  return_pct: number;
  direction: "long" | "short";
  exit_reason: string;
}

export interface BacktestMeta {
  symbol: string;
  primary_timeframe: string;
  filter_timeframe: string | null;
  start: string;
  end: string;
  initial_capital: number;
  direction: "long" | "short";
  defaulted_costs: boolean;
  defaulted_position_sizing: boolean;
}

export interface BacktestRun {
  spec_name: string;
  meta: BacktestMeta;
  equity_curve: EquityPoint[];
  trades: Trade[];
}

export interface BacktestMetrics {
  total_return_pct: number;
  cagr: number;
  annualized_volatility: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  max_drawdown_pct: number;
  max_drawdown_duration_days: number;
  calmar_ratio: number;
  num_trades: number;
  win_rate: number;
  profit_factor: number;
  profit_factor_capped: boolean;
  avg_win_pct: number;
  avg_loss_pct: number;
  expectancy: number;
  largest_win_pct: number;
  largest_loss_pct: number;
  longest_winning_streak: number;
  longest_losing_streak: number;
  avg_trade_duration_days: number;
  exposure_pct: number;
  bars_processed: number;
  bars_per_year: number;
}

export interface BenchmarkResult {
  total_return_pct: number;
  cagr: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  final_value: number;
  initial_value: number;
  equity_curve: BenchmarkEquityPoint[];
}

export interface BenchmarkComparison {
  strategy_return_pct: number;
  benchmark_return_pct: number;
  alpha_pct: number;
  beat_benchmark: boolean;
  strategy_sharpe: number;
  benchmark_sharpe: number;
  risk_adjusted_alpha: number;
  verdict: string;
}

export type AuthorClaimType =
  | "return"
  | "drawdown"
  | "win_rate"
  | "trade_count"
  | "sharpe"
  | "other";

export interface AuthorClaimComparison {
  claim_type: AuthorClaimType;
  author_value_raw: string;
  author_value_parsed: number | null;
  measured_value: number | null;
  measured_label: string;
  difference: number | null;
  discrepancy_ratio: number | null;
  explanation: string;
}

export interface BacktestResult {
  schema_version: "1.0";
  spec_snapshot: Record<string, unknown>;
  run: BacktestRun;
  metrics: BacktestMetrics;
  benchmark: BenchmarkResult;
  benchmark_comparison: BenchmarkComparison;
  author_claim_comparisons: AuthorClaimComparison[];
  data_fetch_seconds: number;
  compute_seconds: number;
}

export interface BacktestStartedResponse {
  job_id: string;
  from_cache: boolean;
  backtest_id: string | null;
}

export interface BacktestRequest {
  start: string;
  end: string;
  initial_capital?: number;
}

// ---- API surface ----------------------------------------------------------

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

export async function postBacktest(
  strategyId: string,
  body: BacktestRequest,
): Promise<BacktestStartedResponse> {
  return jsonFetch<BacktestStartedResponse>(`/strategies/${strategyId}/backtest`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function getBacktest(backtestId: string): Promise<BacktestResult> {
  return jsonFetch<BacktestResult>(`/backtests/${backtestId}`);
}
