"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useThemeColors } from "@/lib/use-theme-colors";
import type { EquityCurvePoint } from "@/lib/trader-api";

/**
 * Single-line equity curve.
 *
 * Mirrors the visual conventions of the existing backtests chart
 * (`app/backtests/[id]/view.tsx`):
 *
 *   - hairline axes, no gridlines (`CartesianGrid` disabled both ways)
 *   - mono tabular-nums ticks via `--font-ibm-plex-mono`
 *   - theme-aware colours from useThemeColors() — the line uses
 *     `ink` so it sits unambiguous against either canvas
 *   - tooltip is hairline-bordered, no shadow, mono content
 *   - `isAnimationActive={false}` per the near-zero-motion rule
 *
 * Equity values come back as strings from the API (psycopg returns
 * Decimal columns that way). We parse them once here.
 */

interface EquityCurveChartProps {
  points: EquityCurvePoint[];
  startingCash: number;
  height?: number;
}

interface ChartRow {
  tsMs: number;
  equity: number;
}

function fmtDate(tsMs: number): string {
  return new Date(tsMs).toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  });
}

function fmtEquity(n: number): string {
  return `£${n.toLocaleString("en-GB", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  })}`;
}

export function EquityCurveChart({
  points,
  startingCash,
  height = 280,
}: EquityCurveChartProps): React.ReactElement {
  const themeColors = useThemeColors();

  const data: ChartRow[] = points.map((p) => ({
    tsMs: Date.parse(p.ts),
    equity: Number.parseFloat(p.equity),
  }));

  // Padded Y domain: 2% breathing room above/below the observed range
  // so the line doesn't kiss the axis. Always include startingCash so
  // the chart's baseline is meaningful before the first big move.
  const equities = data.map((d) => d.equity);
  const min = Math.min(startingCash, ...equities);
  const max = Math.max(startingCash, ...equities);
  const pad = (max - min || 1) * 0.04;
  const yDomain: [number, number] = [min - pad, max + pad];

  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 16, right: 24, bottom: 4, left: 8 }}>
          <CartesianGrid horizontal={false} vertical={false} />
          <XAxis
            dataKey="tsMs"
            type="number"
            domain={["dataMin", "dataMax"]}
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
            tickFormatter={(v: number) => fmtEquity(v)}
            axisLine={{ stroke: themeColors.hairline }}
            tickLine={{ stroke: themeColors.hairline }}
            width={68}
            domain={yDomain}
          />
          <Tooltip
            labelFormatter={(v: number) => fmtDate(v)}
            formatter={(v: number) => fmtEquity(v)}
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
            dataKey="equity"
            stroke={themeColors.ink}
            strokeWidth={1.5}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
