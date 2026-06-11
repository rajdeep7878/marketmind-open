"use client";

/**
 * Editorial Quant treatment of one BacktestResult.
 *
 * Layout (top to bottom):
 *
 *   1. Header band: eyebrow + serif title + muted metadata row.
 *   2. THE HEADLINE COMPARISON: one bold serif verdict sentence
 *      followed by three mono numbers (Strategy / Buy & Hold / Alpha)
 *      separated by hairline vertical dividers.
 *   3. Equity curve chart: ink for strategy, accent for B&H, no
 *      gridlines, hairline axes, mono tabular axis labels.
 *   4. Headline metrics grid: 4-up on desktop, 2 on tablet, 1 on
 *      mobile. Each cell is hairline-bordered; metric name as eyebrow,
 *      value in large mono.
 *   5. Overfitting section.
 *   6. Author claims as editorial table.
 *   7. Trades table, collapsed.
 *
 * All percent fields come from the API as fractions (0.12 == 12%).
 */

import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Breadcrumbs } from "@/components/nav/breadcrumbs";
import { useRecordRecentPage } from "@/lib/use-record-recent-page";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { AuthorClaimComparison, BacktestResult, Trade } from "@/lib/backtest";
import type { OverfittingAnalysis } from "@/lib/overfitting";
import { useThemeColors } from "@/lib/use-theme-colors";

import { OverfittingSection } from "./overfitting-section";

interface Props {
  result: BacktestResult;
  backtestId: string;
  initialOverfittingAnalysis?: OverfittingAnalysis | null;
}

function fmtPct(x: number, opts?: { signed?: boolean; digits?: number }): string {
  const digits = opts?.digits ?? 2;
  const signed = opts?.signed ?? false;
  const v = x * 100;
  return signed ? `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%` : `${v.toFixed(digits)}%`;
}

function fmtNum(x: number, digits = 2): string {
  return x.toFixed(digits);
}

function fmtDate(iso: string): string {
  return iso.slice(0, 10);
}

interface ChartRow {
  ts: string;
  Strategy?: number;
  "Buy & Hold"?: number;
}

function buildChartData(result: BacktestResult): ChartRow[] {
  const byTs = new Map<string, ChartRow>();
  for (const p of result.run.equity_curve) {
    byTs.set(p.timestamp, { ts: p.timestamp, Strategy: p.value });
  }
  for (const p of result.benchmark.equity_curve) {
    const row = byTs.get(p.timestamp) ?? { ts: p.timestamp };
    row["Buy & Hold"] = p.value;
    byTs.set(p.timestamp, row);
  }
  return [...byTs.values()].sort((a, b) => a.ts.localeCompare(b.ts));
}

export function BacktestView({
  result,
  backtestId,
  initialOverfittingAnalysis = null,
}: Props): React.ReactElement {
  const { metrics, benchmark, benchmark_comparison: cmp, run } = result;
  const chartData = useMemo(() => buildChartData(result), [result]);
  const [tradesOpen, setTradesOpen] = useState(false);
  const themeColors = useThemeColors();

  const beat = cmp.beat_benchmark;
  const alphaClass = beat ? "text-positive" : "text-negative";

  // Recent-pages title carries the strategy name + a short id so
  // multiple backtests of the same strategy are distinguishable
  // in the palette.
  useRecordRecentPage({
    path: `/backtests/${backtestId}`,
    title: `${run.spec_name} #${backtestId.slice(0, 8)}`,
  });

  return (
    <main className="mx-auto flex w-full max-w-editorial flex-col px-6 py-20 sm:px-10">
      <Breadcrumbs
        items={[
          { label: "Backtests" },
          { label: `${run.spec_name} #${backtestId.slice(0, 8)}` },
        ]}
      />
      {/* ---- Header ---- */}
      <header className="mt-4 flex flex-col gap-4">
        <div className="flex items-baseline justify-between gap-4">
          <span className="eyebrow">Backtest result</span>
          <code className="font-mono text-[0.7rem] text-muted">{backtestId.slice(0, 8)}…</code>
        </div>
        <h1 className="font-serif text-3xl text-ink sm:text-4xl">{run.spec_name}</h1>
        <p className="mt-1 text-sm text-muted">
          <span className="font-mono tabular-nums">{run.meta.symbol}</span>
          <span className="mx-3 text-hairline">·</span>
          <span className="font-mono tabular-nums">{run.meta.primary_timeframe}</span>
          <span className="mx-3 text-hairline">·</span>
          <span className="font-mono tabular-nums">
            {fmtDate(run.meta.start)} → {fmtDate(run.meta.end)}
          </span>
          <span className="mx-3 text-hairline">·</span>
          <span>
            Initial{" "}
            <span className="font-mono tabular-nums">
              ${run.meta.initial_capital.toLocaleString()}
            </span>
          </span>
        </p>
      </header>

      <Separator className="my-12" />

      {/* ---- Headline comparison ---- */}
      <section>
        <span className="eyebrow">Verdict</span>
        <p className="mt-3 max-w-prose font-serif text-3xl leading-tight text-ink sm:text-4xl">
          {beat ? (
            <>
              Beat buy-and-hold by{" "}
              <span className={`${alphaClass} tabular-nums`}>{fmtPct(cmp.alpha_pct)}</span>.
            </>
          ) : (
            <>
              Underperformed buy-and-hold by{" "}
              <span className={`${alphaClass} tabular-nums`}>
                {fmtPct(Math.abs(cmp.alpha_pct))}
              </span>
              .
            </>
          )}
        </p>
        <p className="mt-4 max-w-prose text-base leading-relaxed text-muted">{cmp.verdict}</p>

        <div className="mt-10 grid grid-cols-1 divide-y divide-hairline border-y border-hairline sm:grid-cols-3 sm:divide-x sm:divide-y-0">
          <HeadlineNumber
            label="Strategy"
            value={fmtPct(cmp.strategy_return_pct, { signed: true })}
            tone={cmp.strategy_return_pct >= 0 ? "positive" : "negative"}
          />
          <HeadlineNumber
            label="Buy & Hold"
            value={fmtPct(cmp.benchmark_return_pct, { signed: true })}
            tone={cmp.benchmark_return_pct >= 0 ? "muted" : "negative"}
          />
          <HeadlineNumber
            label="Alpha"
            value={fmtPct(cmp.alpha_pct, { signed: true })}
            tone={beat ? "positive" : "negative"}
            emphasis
          />
        </div>
      </section>

      <Separator className="my-14" />

      {/* ---- Equity curve ---- */}
      <section>
        <div className="mb-6 flex items-baseline justify-between gap-4">
          <div>
            <span className="eyebrow">Chart</span>
            <h2 className="mt-2 font-serif text-2xl text-ink">
              Equity curve — strategy vs buy-and-hold
            </h2>
          </div>
          <div className="text-right text-xs text-muted">
            <div>Buy & Hold final</div>
            <div className="font-mono text-sm tabular-nums text-ink">
              $
              {benchmark.final_value.toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })}
            </div>
          </div>
        </div>
        <div className="h-80 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 16, right: 24, bottom: 4, left: 8 }}>
              <CartesianGrid horizontal={false} vertical={false} />
              <XAxis
                dataKey="ts"
                tickFormatter={fmtDate}
                tick={{
                  fontSize: 10,
                  fontFamily: "var(--font-ibm-plex-mono)",
                  fill: themeColors.muted,
                }}
                axisLine={{ stroke: themeColors.hairline }}
                tickLine={{ stroke: themeColors.hairline }}
                minTickGap={48}
              />
              <YAxis
                tick={{
                  fontSize: 10,
                  fontFamily: "var(--font-ibm-plex-mono)",
                  fill: themeColors.muted,
                }}
                tickFormatter={(v: number) => `$${Math.round(v).toLocaleString()}`}
                axisLine={{ stroke: themeColors.hairline }}
                tickLine={{ stroke: themeColors.hairline }}
                width={80}
                domain={["auto", "auto"]}
              />
              <Tooltip
                labelFormatter={fmtDate}
                formatter={(v: number) => `$${v.toFixed(2)}`}
                contentStyle={{
                  fontSize: 12,
                  fontFamily: "var(--font-ibm-plex-mono)",
                  backgroundColor: themeColors.surface,
                  color: themeColors.ink,
                  border: `1px solid ${themeColors.hairline}`,
                  borderRadius: 2,
                  boxShadow: "none",
                }}
                cursor={{ stroke: themeColors.hairline, strokeWidth: 1 }}
              />
              <Line
                type="monotone"
                dataKey="Strategy"
                stroke={themeColors.ink}
                strokeWidth={1.5}
                dot={false}
                connectNulls
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="Buy & Hold"
                stroke={themeColors.accent}
                strokeWidth={1.5}
                strokeDasharray="4 3"
                dot={false}
                connectNulls
                isAnimationActive={false}
              />
              {(() => {
                const last = chartData[chartData.length - 1];
                if (!last || last.Strategy === undefined) return null;
                return (
                  <ReferenceDot
                    x={last.ts}
                    y={last.Strategy}
                    r={3}
                    fill={themeColors.ink}
                    stroke={themeColors.ink}
                  />
                );
              })()}
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="mt-4 flex items-center gap-6 text-xs text-muted">
          <Legend swatch="bg-ink" label="Strategy" />
          <Legend swatch="bg-accent" label="Buy & Hold" dashed />
        </div>
      </section>

      <Separator className="my-14" />

      {/* ---- Headline metrics grid ---- */}
      <section>
        <span className="eyebrow">Metrics</span>
        <h2 className="mt-2 font-serif text-2xl text-ink">Headline metrics</h2>
        <div className="mt-8 grid grid-cols-1 gap-px bg-hairline sm:grid-cols-2 lg:grid-cols-4">
          {(
            [
              ["Total return", fmtPct(metrics.total_return_pct, { signed: true })],
              ["CAGR", fmtPct(metrics.cagr, { signed: true })],
              ["Sharpe", fmtNum(metrics.sharpe_ratio)],
              ["Sortino", fmtNum(metrics.sortino_ratio)],
              ["Max drawdown", fmtPct(metrics.max_drawdown_pct)],
              ["DD duration", `${metrics.max_drawdown_duration_days}d`],
              ["Trades", `${metrics.num_trades}`],
              ["Win rate", fmtPct(metrics.win_rate)],
              [
                "Profit factor",
                metrics.profit_factor_capped ? "∞ (capped)" : fmtNum(metrics.profit_factor),
              ],
              ["Calmar", fmtNum(metrics.calmar_ratio)],
              ["Volatility", fmtPct(metrics.annualized_volatility)],
              ["Exposure", fmtPct(metrics.exposure_pct)],
            ] satisfies ReadonlyArray<readonly [string, string]>
          ).map(([label, value]) => (
            <MetricCell key={label} label={label} value={value} />
          ))}
        </div>
      </section>

      <Separator className="my-14" />

      {/* ---- Overfitting ---- */}
      <OverfittingSection backtestId={backtestId} initialAnalysis={initialOverfittingAnalysis} />

      {/* ---- Author claims ---- */}
      <Separator className="my-14" />
      <AuthorClaimsTable claims={result.author_claim_comparisons} />

      {/* ---- Trades ---- */}
      <Separator className="my-14" />
      <section>
        <div className="flex items-baseline justify-between">
          <div>
            <span className="eyebrow">Records</span>
            <h2 className="mt-2 font-serif text-2xl text-ink">Trades ({run.trades.length})</h2>
          </div>
          <Button intent="ghost" size="sm" onClick={() => setTradesOpen((o) => !o)} type="button">
            {tradesOpen ? "Hide" : "Show"}
          </Button>
        </div>
        {tradesOpen ? <TradesTable trades={run.trades} /> : null}
      </section>
    </main>
  );
}

function HeadlineNumber({
  label,
  value,
  tone,
  emphasis = false,
}: {
  label: string;
  value: string;
  tone: "positive" | "negative" | "muted";
  emphasis?: boolean;
}): React.ReactElement {
  const toneClass =
    tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : "text-ink";
  return (
    <div className="px-6 py-6 sm:py-8">
      <span className="eyebrow">{label}</span>
      <p
        className={`mt-3 font-mono tabular-nums ${emphasis ? "text-4xl" : "text-3xl"} ${toneClass}`}
      >
        {value}
      </p>
    </div>
  );
}

function MetricCell({ label, value }: { label: string; value: string }): React.ReactElement {
  return (
    <div className="bg-surface px-6 py-6">
      <span className="eyebrow">{label}</span>
      <p className="mt-2 font-mono text-xl tabular-nums text-ink">{value}</p>
    </div>
  );
}

function Legend({
  swatch,
  label,
  dashed = false,
}: {
  swatch: string;
  label: string;
  dashed?: boolean;
}): React.ReactElement {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden
        className={`inline-block h-px w-6 ${dashed ? "border-t border-dashed" : swatch + " h-[2px]"}`}
        style={dashed ? { borderColor: "var(--color-accent)" } : undefined}
      />
      <span>{label}</span>
    </span>
  );
}

function AuthorClaimsTable({
  claims,
}: {
  claims: AuthorClaimComparison[];
}): React.ReactElement | null {
  if (claims.length === 0) {
    return null;
  }
  return (
    <section>
      <span className="eyebrow">Comparison</span>
      <h2 className="mt-2 font-serif text-2xl text-ink">Author claims vs measured</h2>
      <p className="mt-2 max-w-prose text-sm text-muted">
        Comparing the source&rsquo;s stated performance to what our backtest produced. Multi-asset
        portfolio claims are flagged — those aren&rsquo;t directly comparable.
      </p>
      <div className="mt-6">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Claim</TableHead>
              <TableHead>Author</TableHead>
              <TableHead className="text-right">Measured</TableHead>
              <TableHead className="text-right">Δ</TableHead>
              <TableHead>Notes</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {claims.map((c, i) => (
              <TableRow key={`${c.claim_type}-${i}`}>
                <TableCell className="font-mono text-xs">{c.claim_type}</TableCell>
                <TableCell className="font-mono text-xs">{c.author_value_raw}</TableCell>
                <TableCell numeric className="text-xs">
                  {c.measured_value === null
                    ? "—"
                    : formatMeasured(c.measured_label, c.measured_value)}
                </TableCell>
                <TableCell numeric className="text-xs">
                  {c.discrepancy_ratio === null
                    ? "—"
                    : fmtPct(c.discrepancy_ratio, { signed: true })}
                </TableCell>
                <TableCell className="max-w-prose text-xs leading-relaxed text-muted">
                  {c.explanation}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </section>
  );
}

function formatMeasured(label: string, value: number): string {
  if (label === "total return" || label === "max drawdown" || label === "win rate") {
    return fmtPct(value);
  }
  if (label === "trade count") {
    return `${Math.round(value)}`;
  }
  if (label === "Sharpe ratio") {
    return fmtNum(value);
  }
  return `${value}`;
}

function TradesTable({ trades }: { trades: Trade[] }): React.ReactElement {
  if (trades.length === 0) {
    return <p className="mt-6 text-sm text-muted">(no trades)</p>;
  }
  return (
    <div className="mt-6 max-h-96 overflow-y-auto">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>#</TableHead>
            <TableHead>Entry</TableHead>
            <TableHead>Exit</TableHead>
            <TableHead className="text-right">PnL</TableHead>
            <TableHead className="text-right">Return</TableHead>
            <TableHead>Reason</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {trades.map((t, i) => (
            <TableRow key={i}>
              <TableCell numeric className="text-xs">
                {i + 1}
              </TableCell>
              <TableCell className="font-mono text-xs">{fmtDate(t.entry_time)}</TableCell>
              <TableCell className="font-mono text-xs">{fmtDate(t.exit_time)}</TableCell>
              <TableCell numeric className="text-xs">
                {t.pnl >= 0 ? "+" : ""}
                {t.pnl.toFixed(2)}
              </TableCell>
              <TableCell
                numeric
                className={`text-xs ${t.return_pct >= 0 ? "text-positive" : "text-negative"}`}
              >
                {fmtPct(t.return_pct, { signed: true })}
              </TableCell>
              <TableCell className="font-mono text-xs">{t.exit_reason}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
