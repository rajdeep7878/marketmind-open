"use client";

/**
 * Overfitting analysis section for the /backtests/[id] page.
 *
 * Editorial Quant treatment: a big serif/mono headline score, the
 * verdict in display serif, a 4-up signal contribution grid with
 * eyebrow labels + mono numbers, and four hairline-bordered
 * collapsible detail sections (walk-forward, parameter sweep,
 * Monte Carlo, deflated Sharpe). All chart palettes are restricted
 * to ink / accent / positive / negative — no rainbow.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Button } from "@/components/ui/button";
import {
  getJobProgress,
  getOverfitting,
  getOverfittingForBacktest,
  postOverfitting,
  type OverfittingAnalysis,
  type OverfittingVerdict,
} from "@/lib/overfitting";
import { useThemeColors, type ThemeColors } from "@/lib/use-theme-colors";

interface Props {
  backtestId: string;
  initialAnalysis: OverfittingAnalysis | null;
}

function fmtPct(x: number, opts?: { signed?: boolean }): string {
  const v = x * 100;
  const signed = opts?.signed ?? false;
  return signed ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : `${v.toFixed(2)}%`;
}

function fmtNum(x: number, digits = 2): string {
  return x.toFixed(digits);
}

function verdictMeta(v: OverfittingVerdict): {
  label: string;
  toneClass: string;
} {
  if (v === "likely_robust") {
    return { label: "Likely robust", toneClass: "text-positive" };
  }
  if (v === "likely_overfit") {
    return { label: "Likely overfit", toneClass: "text-negative" };
  }
  return { label: "Mixed signals", toneClass: "text-accent" };
}

export function OverfittingSection({ backtestId, initialAnalysis }: Props): React.ReactElement {
  const [analysis, setAnalysis] = useState<OverfittingAnalysis | null>(initialAnalysis);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ step: string; current: number; total: number } | null>(
    null,
  );
  const [jobId, setJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const interval = setInterval(async () => {
      try {
        const p = await getJobProgress(jobId);
        if (cancelled) return;
        if (p.step && p.current !== null && p.total !== null) {
          setProgress({ step: p.step, current: p.current, total: p.total });
        }
        if (p.status === "finished") {
          clearInterval(interval);
          const wrapper = await getOverfittingForBacktest(backtestId);
          if (cancelled) return;
          if (wrapper) {
            setAnalysis(wrapper.analysis);
          }
          setProgress(null);
          setBusy(false);
          setJobId(null);
        } else if (p.status === "failed") {
          clearInterval(interval);
          if (cancelled) return;
          setError("overfitting job failed");
          setProgress(null);
          setBusy(false);
          setJobId(null);
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    }, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [jobId, backtestId]);

  async function onClick(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const resp = await postOverfitting(backtestId);
      if (resp.from_cache && resp.analysis_id) {
        const fetched = await getOverfitting(resp.analysis_id);
        setAnalysis(fetched);
        setBusy(false);
        return;
      }
      if (!resp.job_id) {
        throw new Error("no job_id and no cache hit");
      }
      setJobId(resp.job_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  if (!analysis) {
    return (
      <section>
        <span className="eyebrow">Stress test</span>
        <h2 className="mt-2 font-serif text-2xl text-ink">Overfitting analysis</h2>
        <p className="mt-3 max-w-prose text-base leading-relaxed text-muted">
          Four checks — walk-forward consistency, parameter robustness, Monte Carlo permutation, and
          a deflated Sharpe — combined into a single 0–100 score. Runs in a few minutes on cached
          data.
        </p>
        <div className="mt-6 flex flex-wrap items-center gap-x-6 gap-y-3">
          <Button type="button" intent="primary" size="lg" onClick={onClick} disabled={busy}>
            {busy ? "Running…" : "Run analysis"}
          </Button>
          {progress ? (
            <span className="text-xs uppercase tracking-eyebrow text-muted">
              Step {progress.current} of {progress.total}: {progress.step.replace(/_/g, " ")}
            </span>
          ) : null}
        </div>
        {error ? <p className="mt-4 text-sm text-negative">{error}</p> : null}
      </section>
    );
  }

  return <OverfittingResults analysis={analysis} />;
}

function OverfittingResults({ analysis }: { analysis: OverfittingAnalysis }): React.ReactElement {
  const meta = verdictMeta(analysis.composite.verdict);
  const score = analysis.composite.score;

  return (
    <section>
      <span className="eyebrow">Stress test</span>
      <h2 className="mt-2 font-serif text-2xl text-ink">Overfitting analysis</h2>

      <div className="mt-10 grid grid-cols-1 gap-10 md:grid-cols-5">
        <div className="md:col-span-2">
          <p className="font-mono text-4xl tabular-nums text-ink sm:text-[5rem] sm:leading-none">
            {score.toFixed(0)}
            <span className="ml-2 text-2xl text-muted sm:text-3xl">/100</span>
          </p>
          <p className={`mt-4 font-serif text-2xl ${meta.toneClass}`}>{meta.label}.</p>
          <p className="mt-2 text-xs uppercase tracking-eyebrow text-muted">
            Range {analysis.composite.confidence_band_low.toFixed(0)}–
            {analysis.composite.confidence_band_high.toFixed(0)} (confidence band)
          </p>
        </div>
        <div className="md:col-span-3 md:border-l md:border-hairline md:pl-10">
          <span className="eyebrow">What this means</span>
          <p className="mt-3 max-w-prose font-serif text-lg leading-relaxed text-ink">
            {analysis.composite.explanation}
          </p>
        </div>
      </div>

      {/* Signal contributions */}
      <div className="mt-12 grid grid-cols-1 gap-px bg-hairline sm:grid-cols-2 lg:grid-cols-4">
        {analysis.composite.contributions.map((c) => (
          <div key={c.name} className="bg-surface px-6 py-5">
            <span className="eyebrow">{c.label}</span>
            <p className="mt-2 font-mono text-2xl tabular-nums text-ink">
              {c.contribution_pts.toFixed(0)}
              <span className="ml-1 text-sm text-muted">pts</span>
            </p>
            <p className="mt-1 text-xs text-muted">raw {c.raw_value.toFixed(2)}</p>
          </div>
        ))}
      </div>

      {/* Detail panels */}
      <div className="mt-10 flex flex-col gap-4">
        <DetailSection title="Walk-forward">
          <WalkForwardDetail analysis={analysis} />
        </DetailSection>
        <DetailSection title="Parameter robustness">
          <ParameterSweepDetail analysis={analysis} />
        </DetailSection>
        <DetailSection title="Monte Carlo permutation">
          <MonteCarloDetail analysis={analysis} />
        </DetailSection>
        <DetailSection title="Deflated Sharpe">
          <DeflatedSharpeDetail analysis={analysis} />
        </DetailSection>
      </div>
    </section>
  );
}

function DetailSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}): React.ReactElement {
  const [open, setOpen] = useState(false);
  return (
    <div className="border border-hairline">
      <button
        type="button"
        className="flex w-full items-center justify-between bg-surface px-5 py-4 text-left font-sans text-sm text-ink hover:bg-fill"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="font-serif text-base">{title}</span>
        <span className="eyebrow text-muted">{open ? "Hide" : "Show"}</span>
      </button>
      {open ? (
        <div className="border-t border-hairline bg-surface px-5 py-5">{children}</div>
      ) : null}
    </div>
  );
}

function chartStyles(c: ThemeColors): {
  tick: { fontSize: number; fontFamily: string; fill: string };
  axis: { stroke: string };
  tooltip: {
    fontSize: number;
    fontFamily: string;
    backgroundColor: string;
    color: string;
    border: string;
    borderRadius: number;
    boxShadow: "none";
  };
} {
  return {
    tick: { fontSize: 10, fontFamily: "var(--font-ibm-plex-mono)", fill: c.muted },
    axis: { stroke: c.hairline },
    tooltip: {
      fontSize: 12,
      fontFamily: "var(--font-ibm-plex-mono)",
      backgroundColor: c.surface,
      color: c.ink,
      border: `1px solid ${c.hairline}`,
      borderRadius: 2,
      boxShadow: "none",
    },
  };
}

function WalkForwardDetail({ analysis }: { analysis: OverfittingAnalysis }): React.ReactElement {
  const wf = analysis.walk_forward;
  const colors = useThemeColors();
  const styles = chartStyles(colors);
  const data = useMemo(
    () =>
      wf.windows.map((w) => ({
        name: `W${w.window_index + 1}`,
        "In-sample": w.in_sample_return_pct * 100,
        "Out-of-sample": w.out_of_sample_return_pct * 100,
      })),
    [wf.windows],
  );
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Kv
          k="Degradation"
          v={wf.degradation_ratio_valid ? wf.degradation_ratio.toFixed(2) : "n/a"}
        />
        <Kv k="OOS positive rate" v={fmtPct(wf.out_of_sample_positive_rate)} />
        <Kv k="Consistency" v={fmtNum(wf.consistency_score)} />
        <Kv k="Windows" v={`${wf.n_windows_actual} / ${wf.n_windows_requested}`} />
      </div>
      <div className="h-56 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 4 }}>
            <XAxis
              dataKey="name"
              tick={styles.tick}
              axisLine={styles.axis}
              tickLine={styles.axis}
            />
            <YAxis
              tick={styles.tick}
              tickFormatter={(v: number) => `${v.toFixed(0)}%`}
              axisLine={styles.axis}
              tickLine={styles.axis}
              width={48}
            />
            <Tooltip
              formatter={(v: number) => `${v.toFixed(2)}%`}
              contentStyle={styles.tooltip}
              cursor={{ fill: "transparent" }}
            />
            <Bar dataKey="In-sample" fill={colors.ink} />
            <Bar dataKey="Out-of-sample" fill={colors.accent} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="flex gap-6 text-xs text-muted">
        <LegendSwatch swatch="bg-ink" label="In-sample" />
        <LegendSwatch swatch="bg-accent" label="Out-of-sample" />
      </div>
    </div>
  );
}

function ParameterSweepDetail({ analysis }: { analysis: OverfittingAnalysis }): React.ReactElement {
  const sw = analysis.parameter_sweep;
  const colors = useThemeColors();
  const styles = chartStyles(colors);
  const sortedCells = useMemo(
    () => [...sw.cells].sort((a, b) => a.total_return_pct - b.total_return_pct),
    [sw.cells],
  );
  if (sw.n_combinations === 0) {
    return (
      <p className="text-sm text-muted">
        No parameter sweep ran ({sw.skipped_reason ?? "no swept-eligible parameters"}).
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Kv k="Peakiness" v={fmtNum(sw.peakiness_score)} />
        <Kv k="Baseline pct-ile" v={fmtPct(sw.baseline_rank_percentile)} />
        <Kv k="Best" v={fmtPct(sw.best_in_grid_return, { signed: true })} />
        <Kv k="Worst" v={fmtPct(sw.worst_in_grid_return, { signed: true })} />
      </div>
      <p className="text-xs text-muted">
        Axes swept: {sw.axes.map((a) => a.label).join(", ")}.
        {sw.skipped_reason ? <span className="ml-1 italic">{sw.skipped_reason}</span> : null}
      </p>
      <div className="h-56 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={sortedCells.map((c, i) => ({
              name: `#${i + 1}`,
              return: c.total_return_pct * 100,
              isBaseline: c.is_baseline,
            }))}
            margin={{ top: 8, right: 8, bottom: 4, left: 4 }}
          >
            <XAxis
              dataKey="name"
              tick={styles.tick}
              axisLine={styles.axis}
              tickLine={styles.axis}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={styles.tick}
              tickFormatter={(v: number) => `${v.toFixed(0)}%`}
              axisLine={styles.axis}
              tickLine={styles.axis}
              width={48}
            />
            <Tooltip
              formatter={(v: number) => `${v.toFixed(2)}%`}
              contentStyle={styles.tooltip}
              cursor={{ fill: "transparent" }}
            />
            <Bar dataKey="return">
              {sortedCells.map((c, i) => (
                <Cell key={i} fill={c.is_baseline ? colors.accent : colors.ink} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="flex gap-6 text-xs text-muted">
        <LegendSwatch swatch="bg-ink" label="Other cells" />
        <LegendSwatch swatch="bg-accent" label="Baseline (this spec)" />
      </div>
    </div>
  );
}

function MonteCarloDetail({ analysis }: { analysis: OverfittingAnalysis }): React.ReactElement {
  const mc = analysis.monte_carlo;
  const colors = useThemeColors();
  const styles = chartStyles(colors);
  const data = useMemo(
    () =>
      mc.histogram.map((b) => ({
        midpoint: ((b.lo + b.hi) / 2) * 100,
        count: b.count,
      })),
    [mc.histogram],
  );
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Kv k="p-value" v={fmtNum(mc.p_value)} />
        <Kv k="Percentile rank" v={fmtPct(mc.percentile_rank)} />
        <Kv k="Permutations" v={`${mc.n_permutations}`} />
        <Kv k="Real return" v={fmtPct(mc.real_return_pct, { signed: true })} />
      </div>
      <div className="h-56 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 4 }}>
            <XAxis
              dataKey="midpoint"
              tick={styles.tick}
              tickFormatter={(v: number) => `${v.toFixed(0)}%`}
              axisLine={styles.axis}
              tickLine={styles.axis}
            />
            <YAxis
              tick={styles.tick}
              allowDecimals={false}
              axisLine={styles.axis}
              tickLine={styles.axis}
              width={32}
            />
            <Tooltip
              formatter={(v: number, name: string) =>
                name === "count" ? `${v} runs` : `${(v as number).toFixed(2)}%`
              }
              labelFormatter={(label: number) => `~${label.toFixed(2)}%`}
              contentStyle={styles.tooltip}
              cursor={{ fill: "transparent" }}
            />
            <ReferenceLine
              x={mc.real_return_pct * 100}
              stroke={colors.accent}
              strokeWidth={1.5}
              label={{
                value: "real",
                fontSize: 10,
                fill: colors.accent,
                fontFamily: "var(--font-ibm-plex-mono)",
              }}
            />
            <Bar dataKey="count" fill={colors.ink} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="text-xs text-muted">
        Accent line marks the strategy&rsquo;s real return; bars are synthetic returns from shuffled
        data.
      </p>
    </div>
  );
}

function DeflatedSharpeDetail({ analysis }: { analysis: OverfittingAnalysis }): React.ReactElement {
  const d = analysis.deflated_sharpe;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
        <Kv k="Observed Sharpe" v={fmtNum(d.observed_sharpe)} />
        <Kv k="E[max SR | N trials]" v={fmtNum(d.expected_max_sharpe)} />
        <Kv k="Deflated Sharpe" v={fmtNum(d.deflated_sharpe_ratio)} />
        <Kv k="Prob. real" v={fmtPct(d.probability_strategy_is_real)} />
        <Kv k="Trials est." v={`${d.n_trials_estimate}`} />
        <Kv k="Sample size" v={`${d.n_observations}`} />
      </div>
      <p className="text-xs text-muted">
        Method: <code className="font-mono">{d.method}</code>. Assumes σ(SR trials) = 1 and trial
        Sharpes ~ N(0, 1).
      </p>
    </div>
  );
}

function Kv({ k, v }: { k: string; v: string }): React.ReactElement {
  return (
    <div className="flex flex-col gap-1">
      <span className="eyebrow">{k}</span>
      <span className="font-mono text-sm tabular-nums text-ink">{v}</span>
    </div>
  );
}

function LegendSwatch({ swatch, label }: { swatch: string; label: string }): React.ReactElement {
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden className={`inline-block h-[2px] w-6 ${swatch}`} />
      <span>{label}</span>
    </span>
  );
}
