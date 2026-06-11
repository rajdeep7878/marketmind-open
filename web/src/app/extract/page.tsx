"use client";

/**
 * /extract — URL submission form.
 *
 * Editorial Quant treatment: an eyebrow / serif headline / muted body /
 * single mono URL field / single accent button. Status messages render
 * as hairline callouts beneath the form. No shadows, no rounded inputs.
 *
 * Flow:
 *   1. POST /content/ingest                  → wait for job to finish
 *   2. POST /content/{id}/transcribe         → wait for job to finish
 *      (only if the source isn't raw_text)
 *   3. POST /content/{id}/extract            → wait for job, OR
 *                                              short-circuit on
 *                                              from_cache=true
 *   4. router.push(/strategies/{strategy_id})
 */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Breadcrumbs } from "@/components/nav/breadcrumbs";
import { useRecordRecentPage } from "@/lib/use-record-recent-page";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { postExtract, postIngest, postTranscribe, pollJob } from "@/lib/extraction";

type Step = "idle" | "ingesting" | "transcribing" | "extracting" | "redirecting" | "error";

function inferIsYoutube(url: string): boolean {
  return /^(https?:\/\/)?(www\.|m\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/i.test(
    url.trim(),
  );
}

export default function ExtractPage(): React.ReactElement {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [step, setStep] = useState<Step>("idle");
  const [message, setMessage] = useState<string>("");

  const busy = step !== "idle" && step !== "error";

  async function onSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    if (!url.trim()) return;

    try {
      setStep("ingesting");
      setMessage("Downloading source…");
      const ingestJob = await postIngest(url);
      const ingestDone = await pollJob(ingestJob.id);
      if (ingestDone.status !== "finished" || !ingestDone.result) {
        throw new Error(`Ingestion failed: ${ingestDone.error ?? "unknown error"}`);
      }
      const contentId = ingestDone.result["content_id"] as string;

      if (inferIsYoutube(url)) {
        setStep("transcribing");
        setMessage("Transcribing audio (this can take a minute on a long video)…");
        const transcribeJob = await postTranscribe(contentId);
        const transcribeDone = await pollJob(transcribeJob.id, {
          timeoutMs: 10 * 60 * 1000,
        });
        if (transcribeDone.status !== "finished") {
          throw new Error(`Transcription failed: ${transcribeDone.error ?? "unknown error"}`);
        }
      }

      setStep("extracting");
      setMessage("Asking the model to extract the strategy…");
      const extract = await postExtract(contentId);

      let strategyId: string | null = extract.extraction_id;
      if (!strategyId) {
        const extractDone = await pollJob(extract.job_id);
        if (extractDone.status !== "finished" || !extractDone.result) {
          throw new Error(`Extraction failed: ${extractDone.error ?? "unknown error"}`);
        }
        strategyId = extractDone.result["extraction_id"] as string;
      }

      setStep("redirecting");
      setMessage("Done. Loading the report…");
      router.push(`/strategies/${strategyId}`);
    } catch (err) {
      setStep("error");
      setMessage(err instanceof Error ? err.message : String(err));
    }
  }

  useRecordRecentPage({ path: "/extract", title: "Analyse a strategy" });

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-2xl flex-col justify-center px-6 py-24 sm:px-8">
      <Breadcrumbs items={[{ label: "Analyse" }]} />
      <header className="mt-4 flex flex-col gap-4">
        <span className="eyebrow">Submit</span>
        <h1 className="font-serif text-3xl text-ink sm:text-4xl">Analyse a strategy</h1>
        <p className="max-w-prose font-serif text-lg leading-relaxed text-muted">
          Paste a YouTube video or article URL. We download it, transcribe it if it&rsquo;s audio,
          and ask the model whether the strategy inside it can be mechanically backtested.
        </p>
      </header>

      <Separator className="my-10" />

      <form onSubmit={onSubmit} className="flex flex-col gap-5">
        <label className="eyebrow" htmlFor="url-input">
          Source URL
        </label>
        <input
          id="url-input"
          type="url"
          required
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://youtu.be/… or https://example.com/article"
          disabled={busy}
          className="h-14 w-full rounded-sm border border-hairline bg-surface px-4 font-mono text-base text-ink placeholder:text-muted/60 focus:border-accent focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50"
        />
        <div className="flex flex-wrap items-baseline gap-x-6 gap-y-3">
          <Button type="submit" intent="primary" size="lg" disabled={busy || !url.trim()}>
            {busy ? "Working…" : "Analyse"}
          </Button>
          <span className="text-xs text-muted">
            Crypto strategies only in v1. ~2–5 minutes per analysis.
          </span>
        </div>
      </form>

      {step !== "idle" ? (
        <section
          aria-live="polite"
          role="status"
          className={
            step === "error"
              ? "callout mt-10 font-sans text-sm text-ink"
              : "callout callout-positive mt-10 font-sans text-sm text-ink"
          }
        >
          <p className="eyebrow mb-2">{step === "error" ? "Error" : "Status"}</p>
          <p className="font-serif text-base capitalize text-ink">{step.replace(/_/g, " ")}</p>
          <p className="mt-2 break-words text-sm text-muted">{message}</p>
        </section>
      ) : null}
    </main>
  );
}
