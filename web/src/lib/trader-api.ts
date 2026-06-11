/**
 * Typed fetchers for /trader/* read endpoints + minimal response
 * type interfaces. Hand-rolled (the generated types in
 * src/types/generated/schemas.ts aren't committed; see lib/api.ts
 * for the same pattern).
 *
 * Mirrors the field shapes in api/src/marketmind_api/routes/trader.py.
 * Keep in lockstep with that file when fields change.
 */

import { apiBaseUrl } from "@/lib/env";

// ---- Response shapes ------------------------------------------------------

export interface BotRunSummary {
  id: string;
  loop_name: "ingestion" | "signal_execution" | "runner";
  status: "running" | "stopped" | "crashed";
  started_at: string;
  last_heartbeat_at: string;
  notes: string;
}

export interface HealthResponse {
  latest_run: BotRunSummary | null;
  last_snapshot_ts: string | null;
  now: string;
}

export interface PortfolioSnapshot {
  id: number;
  ts: string;
  cash: string;
  equity: string;
  unrealised_pnl: string;
  realised_pnl_cumulative: string;
  peak_equity: string;
  drawdown: string;
  drawdown_pct: string;
  open_positions_count: number;
  per_strategy_breakdown: Record<string, unknown>;
  per_symbol_breakdown: Record<string, unknown>;
}

export interface EquityCurvePoint {
  ts: string;
  equity: string;
}

export interface EquityCurveResponse {
  points: EquityCurvePoint[];
}

export interface PaperPosition {
  id: string;
  strategy_version_id: string;
  symbol: string;
  side: "LONG" | "SHORT";
  entry_order_id: string;
  exit_order_id: string | null;
  entry_price: string;
  entry_ts: string;
  exit_price: string | null;
  exit_ts: string | null;
  size: string;
  stop_price: string;
  take_profit_price: string | null;
  status: "OPEN" | "CLOSED";
  realised_pnl: string | null;
  realised_pnl_pct: string | null;
  close_reason: string | null;
}

export interface PaperPositionList {
  items: PaperPosition[];
}

export interface PaperPositionListPaginated extends PaperPositionList {
  limit: number;
  offset: number;
}

export interface Signal {
  id: string;
  strategy_version_id: string;
  symbol: string;
  timeframe: string;
  candle_close_ts: string;
  signal: "BUY" | "SELL" | "HOLD" | "EXIT";
  reason: string;
  indicators: Record<string, unknown>;
  proposed_entry_price: string | null;
  proposed_stop_price: string | null;
  proposed_take_profit_price: string | null;
  created_at: string;
  processed_at: string | null;
}

export interface SignalList {
  items: Signal[];
  limit: number;
  offset: number;
}

export interface AlertItem {
  id: string;
  ts: string;
  channel: "telegram" | "log";
  severity: "info" | "warning" | "critical";
  subject: string;
  body: string;
  delivered: boolean;
  delivery_error: string | null;
}

export interface AlertList {
  items: AlertItem[];
  limit: number;
  offset: number;
}

export interface AuditLogItem {
  id: number;
  ts: string;
  actor: string;
  event: string;
  entity_type: string;
  entity_id: string | null;
  payload: Record<string, unknown>;
}

export interface AuditList {
  items: AuditLogItem[];
  limit: number;
  offset: number;
}

export interface StrategyVersionSummary {
  id: string;
  strategy_id: string;
  version: number;
  template: string;
  symbols: string[];
  timeframes: string[];
  risk_pct: string;
  enabled: boolean;
  approved_for_paper: boolean;
  created_at: string;
  latest_drift_health: "healthy" | "watch" | "breach" | null;
  latest_drift_ts: string | null;
  latest_drift_window: string | null;
}

export interface StrategyVersionList {
  items: StrategyVersionSummary[];
}

export interface DriftMetric {
  id: string;
  ts: string;
  strategy_version_id: string;
  window_label: string;
  paper_trade_count: number;
  paper_win_rate: string;
  paper_avg_return_per_trade: string;
  paper_current_drawdown_pct: string;
  backtest_trade_freq_per_week: string;
  backtest_win_rate: string;
  backtest_avg_return_per_trade: string;
  backtest_max_drawdown_pct: string;
  trade_freq_ratio: string;
  win_rate_delta: string;
  avg_return_delta: string;
  drawdown_ratio: string;
  health_status: "healthy" | "watch" | "breach";
}

export interface DriftList {
  items: DriftMetric[];
}

export interface RiskEventLite {
  id: string;
  ts: string;
  event_type: string;
  severity: "info" | "warning" | "critical";
  strategy_version_id: string | null;
  symbol: string | null;
  details: Record<string, unknown>;
}

export interface RiskStatusResponse {
  cash: string | null;
  equity: string | null;
  drawdown_pct: string | null;
  peak_equity: string | null;
  kill_switch_tripped: boolean;
  last_snapshot_ts: string | null;
  recent_risk_events: RiskEventLite[];
}

// ---- Fetcher --------------------------------------------------------------

/**
 * Generic GET fetcher used by the dashboard's polling hook. Returns
 * the parsed body on 2xx; throws on non-2xx so the hook can surface
 * "Couldn't load — retrying".
 */
export async function traderFetch<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${apiBaseUrl()}${path}`, {
    cache: "no-store",
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) {
    throw new Error(`GET ${path} -> ${res.status}: ${await res.text()}`);
  }
  return (await res.json()) as T;
}
