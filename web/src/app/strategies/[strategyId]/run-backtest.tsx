"use client";

/**
 * "Run Backtest" form on the strategy page.
 *
 * Editorial Quant treatment: eyebrow + serif headline, three hairline-
 * bordered mono inputs in a single row, single accent button. No
 * surface card — the section is on-page rather than enclosed.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { postBacktest, type BacktestRequest } from "@/lib/backtest";
import { getJob, pollJob } from "@/lib/extraction";

interface Props {
  strategyId: string;
}

function isoToInput(date: Date): string {
  const m = String(date.getUTCMonth() + 1).padStart(2, "0");
  const d = String(date.getUTCDate()).padStart(2, "0");
  return `${date.getUTCFullYear()}-${m}-${d}`;
}

function inputToIso(value: string): string {
  return new Date(`${value}T00:00:00Z`).toISOString();
}

// Earliest date we expect to have cached OHLCV for any whitelisted
// instrument. BTC/USDT goes back to ~2020-05 on Binance; we pick a
// conservative pre-roll so most pairs and most start dates are usable.
// The backend rejects ranges with no data, so an over-eager default
// here surfaces as a clean error rather than silent failure.
const _DEFAULT_START_ISO_DATE = "2020-01-01";

export function RunBacktestForm({ strategyId }: Props): React.ReactElement {
  const router = useRouter();
  const today = new Date();

  const [start, setStart] = useState(_DEFAULT_START_ISO_DATE);
  const [end, setEnd] = useState(isoToInput(today));
  const [capital, setCapital] = useState("10000");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    setError(null);
    setBusy(true);
    setProgress("Starting backtest…");
    try {
      const body: BacktestRequest = {
        start: inputToIso(start),
        end: inputToIso(end),
        initial_capital: Number.parseFloat(capital) || 10_000,
      };
      const resp = await postBacktest(strategyId, body);
      if (resp.from_cache && resp.backtest_id) {
        router.push(`/backtests/${resp.backtest_id}`);
        return;
      }
      if (!resp.job_id) {
        throw new Error("no job_id returned");
      }
      setProgress("Running engine…");
      const job = await pollJob(resp.job_id);
      if (job.status !== "finished") {
        throw new Error(job.error ?? "job failed");
      }
      const final = await getJob(resp.job_id);
      const bid = final.result?.backtest_id;
      if (typeof bid !== "string") {
        throw new Error("job finished but no backtest_id in result");
      }
      router.push(`/backtests/${bid}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }

  return (
    <section>
      <span className="eyebrow">Action</span>
      <h2 className="mt-2 font-serif text-2xl text-ink">Run a backtest</h2>
      <p className="mt-2 max-w-prose text-sm text-muted">
        Single-instrument, with real fees + slippage + a buy-and-hold benchmark on the same period.
      </p>
      <form onSubmit={onSubmit} className="mt-8 grid grid-cols-1 gap-x-6 gap-y-5 sm:grid-cols-12">
        <Field label="Start" className="sm:col-span-3">
          <input
            type="date"
            value={start}
            onChange={(e) => setStart(e.target.value)}
            required
            className="h-11 w-full border-0 border-b border-hairline bg-transparent px-0 font-mono text-base tabular-nums text-ink focus:border-accent focus:outline-none focus:ring-0"
          />
        </Field>
        <Field label="End" className="sm:col-span-3">
          <input
            type="date"
            value={end}
            onChange={(e) => setEnd(e.target.value)}
            required
            className="h-11 w-full border-0 border-b border-hairline bg-transparent px-0 font-mono text-base tabular-nums text-ink focus:border-accent focus:outline-none focus:ring-0"
          />
        </Field>
        <Field label="Initial capital ($)" className="sm:col-span-3">
          <input
            type="number"
            min="100"
            max="10000000"
            step="1"
            value={capital}
            onChange={(e) => setCapital(e.target.value)}
            required
            className="h-11 w-full border-0 border-b border-hairline bg-transparent px-0 font-mono text-base tabular-nums text-ink focus:border-accent focus:outline-none focus:ring-0"
          />
        </Field>
        <div className="flex items-end sm:col-span-3">
          <Button
            type="submit"
            intent="primary"
            size="lg"
            disabled={busy}
            className="w-full sm:w-auto"
          >
            {busy ? "Running…" : "Run backtest"}
          </Button>
        </div>
      </form>
      {progress ? (
        <p className="mt-4 text-xs uppercase tracking-eyebrow text-muted">{progress}</p>
      ) : null}
      {error ? <p className="mt-4 text-sm text-negative">{error}</p> : null}
    </section>
  );
}

function Field({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <label className={`flex flex-col gap-2 ${className ?? ""}`}>
      <span className="eyebrow">{label}</span>
      {children}
    </label>
  );
}
