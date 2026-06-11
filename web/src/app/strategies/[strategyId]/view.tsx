"use client";

/**
 * Pure-presentation component for one ExtractionResult.
 *
 * Editorial Quant treatment: top-to-bottom longform article. Eyebrow,
 * serif title with the strategy name, hero verdict statement, summary
 * + reasoning as serif body prose, hairline callout for refusals,
 * editorial table for extracted rules, two-column hairline-divided
 * backtestable/not-backtestable list, run-backtest form, and a
 * collapsed StrategySpec JSON block.
 */

import { useState } from "react";

import { Breadcrumbs } from "@/components/nav/breadcrumbs";
import { useRecordRecentPage } from "@/lib/use-record-recent-page";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { ExtractionResult, ExtractionVerdict } from "@/lib/extraction";

import { RunBacktestForm } from "./run-backtest";

interface Props {
  result: ExtractionResult;
  strategyId: string;
}

const VERDICT_COPY: Record<
  ExtractionVerdict,
  { label: string; tone: "accent" | "muted" | "negative" }
> = {
  fully_extractable: { label: "Fully extractable", tone: "accent" },
  partially_extractable: { label: "Partially extractable", tone: "accent" },
  not_extractable: { label: "Not extractable", tone: "negative" },
  not_a_strategy: { label: "Not a strategy", tone: "muted" },
};

function specName(spec: ExtractionResult["spec"]): string {
  if (spec && typeof spec === "object" && "name" in spec && typeof spec.name === "string") {
    return spec.name;
  }
  return "Extraction report";
}

export function StrategyView({ result, strategyId }: Props): React.ReactElement {
  const { spec, report } = result;
  const verdict = VERDICT_COPY[report.verdict];
  const confidencePct = Math.round(report.overall_confidence * 100);
  const verdictTextClass =
    verdict.tone === "accent"
      ? "text-accent"
      : verdict.tone === "negative"
        ? "text-negative"
        : "text-muted";

  const [jsonOpen, setJsonOpen] = useState(false);
  const title = specName(spec);

  // Record this page in the recent-pages list. Re-fires when the
  // fetched title changes so the palette's RECENT section sees
  // the real strategy name rather than the placeholder.
  useRecordRecentPage({
    path: `/strategies/${strategyId}`,
    title,
  });

  return (
    <main className="mx-auto flex w-full max-w-editorial flex-col px-6 py-20 sm:px-10">
      <Breadcrumbs
        items={[
          { label: "Strategies" },
          { label: title },
        ]}
      />
      {/* ---- Header ---- */}
      <header className="mt-4 flex flex-col gap-4">
        <div className="flex items-baseline justify-between gap-4">
          <span className="eyebrow">Extraction report</span>
          <code className="font-mono text-[0.7rem] text-muted">{strategyId.slice(0, 8)}…</code>
        </div>
        <h1 className="font-serif text-3xl text-ink sm:text-4xl">{title}</h1>
      </header>

      <Separator className="my-10" />

      {/* ---- Hero verdict block (60/40 split) ---- */}
      <section className="grid grid-cols-1 gap-10 md:grid-cols-5">
        <div className="md:col-span-3">
          <span className="eyebrow">Verdict</span>
          <p
            className={`mt-3 font-serif text-3xl leading-tight tracking-tight sm:text-4xl ${verdictTextClass}`}
          >
            {verdict.label}.
          </p>
          <p className="mt-6 max-w-prose font-serif text-lg leading-relaxed text-ink">
            {report.summary}
          </p>
        </div>
        <div className="md:col-span-2 md:border-l md:border-hairline md:pl-10">
          <span className="eyebrow">Confidence</span>
          <p className="mt-3 font-mono text-4xl tabular-nums text-ink">
            {confidencePct}
            <span className="text-2xl text-muted">%</span>
          </p>
          <div className="mt-4 h-px w-full bg-hairline" />
          <div
            role="progressbar"
            aria-label="overall confidence"
            aria-valuenow={confidencePct}
            aria-valuemin={0}
            aria-valuemax={100}
            className="-mt-px h-px bg-ink"
            style={{ width: `${confidencePct}%` }}
          />
          <p className="mt-3 max-w-prose text-xs text-muted">
            How confident the model was that the extracted rules faithfully represent the source.
          </p>
        </div>
      </section>

      <Separator className="my-14" />

      {/* ---- Reasoning ---- */}
      <section className="grid grid-cols-1 gap-10 md:grid-cols-5">
        <div className="md:col-span-1">
          <span className="eyebrow">Reasoning</span>
        </div>
        <div className="md:col-span-4">
          <p className="max-w-prose font-serif text-lg leading-relaxed text-ink">
            {report.reasoning}
          </p>
        </div>
      </section>

      {/* ---- Refusal callout ---- */}
      {spec === null && report.refusal_explanation ? (
        <>
          <Separator className="my-14" />
          <section className="callout">
            <p className="eyebrow mb-2 text-negative">Why we refused</p>
            <p className="max-w-prose font-serif text-base leading-relaxed text-ink">
              {report.refusal_explanation}
            </p>
          </section>
        </>
      ) : null}

      {/* ---- Extracted rules ---- */}
      {report.extracted_rules.length > 0 && (
        <>
          <Separator className="my-14" />
          <section>
            <div className="mb-6 flex items-baseline justify-between">
              <h2 className="font-serif text-2xl text-ink">Extracted rules</h2>
              <span className="text-xs text-muted">{report.extracted_rules.length} rules</span>
            </div>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Field</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead className="text-right">Mechanical</TableHead>
                  <TableHead className="text-right">Conf.</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.extracted_rules.map((r, i) => (
                  <TableRow key={`${r.field}-${i}`}>
                    <TableCell className="font-mono text-xs">{r.field}</TableCell>
                    <TableCell className="text-sm">{r.value_description}</TableCell>
                    <TableCell numeric>{r.extractable ? "yes" : "no"}</TableCell>
                    <TableCell numeric>{Math.round(r.confidence * 100)}%</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </section>
        </>
      )}

      {/* ---- Author claims ---- */}
      {report.author_claims.length > 0 && (
        <>
          <Separator className="my-14" />
          <section>
            <div className="mb-6 flex flex-col gap-1">
              <h2 className="font-serif text-2xl text-ink">Author claims</h2>
              <p className="text-sm text-muted">
                What the source said about results. The backtester compares these against measured
                performance.
              </p>
            </div>
            <ul className="flex flex-col gap-6">
              {report.author_claims.map((c, i) => (
                <li
                  key={`${c.claim_type}-${i}`}
                  className="grid grid-cols-1 gap-3 border-l border-hairline pl-6 md:grid-cols-5 md:gap-6"
                >
                  <div className="md:col-span-1">
                    <span className="eyebrow">{c.claim_type.replace(/_/g, " ")}</span>
                    <p className="mt-1 font-mono text-sm tabular-nums text-ink">{c.value}</p>
                  </div>
                  <p className="font-serif text-base italic leading-relaxed text-muted md:col-span-4">
                    &ldquo;{c.quote}&rdquo;
                  </p>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}

      {/* ---- Backtestable / not-backtestable ---- */}
      {(report.backtestable_parts.length > 0 || report.non_backtestable_parts.length > 0) && (
        <>
          <Separator className="my-14" />
          <section className="grid grid-cols-1 gap-10 md:grid-cols-2 md:divide-x md:divide-hairline">
            <div className="md:pr-10">
              <span className="eyebrow">Mechanical</span>
              <h3 className="mt-2 font-serif text-xl text-ink">Backtestable parts</h3>
              <ul className="mt-4 flex flex-col gap-3">
                {report.backtestable_parts.map((p, i) => (
                  <li key={i} className="flex gap-3 text-sm leading-relaxed text-ink">
                    <span aria-hidden className="text-positive">
                      —
                    </span>
                    <span>{p}</span>
                  </li>
                ))}
                {report.backtestable_parts.length === 0 && (
                  <li className="text-sm text-muted">(none)</li>
                )}
              </ul>
            </div>
            <div className="md:pl-10">
              <span className="eyebrow">Discretionary</span>
              <h3 className="mt-2 font-serif text-xl text-ink">Not backtestable</h3>
              <ul className="mt-4 flex flex-col gap-3">
                {report.non_backtestable_parts.map((p, i) => (
                  <li key={i} className="flex gap-3 text-sm leading-relaxed text-ink">
                    <span aria-hidden className="text-negative">
                      —
                    </span>
                    <span>{p}</span>
                  </li>
                ))}
                {report.non_backtestable_parts.length === 0 && (
                  <li className="text-sm text-muted">(none)</li>
                )}
              </ul>
            </div>
          </section>
        </>
      )}

      {/* ---- Run backtest ---- */}
      {spec !== null && (
        <>
          <Separator className="my-14" />
          <RunBacktestForm strategyId={strategyId} />
        </>
      )}

      {/* ---- StrategySpec JSON ---- */}
      {spec !== null && (
        <>
          <Separator className="my-14" />
          <section>
            <div className="flex items-baseline justify-between gap-4">
              <h2 className="font-serif text-2xl text-ink">StrategySpec</h2>
              <button
                type="button"
                onClick={() => setJsonOpen((o) => !o)}
                className="text-xs uppercase tracking-eyebrow text-muted underline decoration-hairline decoration-1 underline-offset-4 hover:text-ink hover:decoration-ink"
              >
                {jsonOpen ? "Hide JSON" : "Show JSON"}
              </button>
            </div>
            {jsonOpen ? (
              <pre className="mt-6 max-h-[32rem] overflow-auto rounded-sm border border-hairline bg-fill p-6 font-mono text-xs leading-relaxed text-ink">
                {JSON.stringify(spec, null, 2)}
              </pre>
            ) : (
              <p className="mt-3 text-sm text-muted">
                The full canonical JSON the engine receives. Click &ldquo;Show JSON&rdquo; to
                inspect.
              </p>
            )}
          </section>
        </>
      )}
    </main>
  );
}
