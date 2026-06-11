import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";

/**
 * Three receipt cards link to real analyses we already ran. The IDs
 * are hardcoded so this page renders without a DB round-trip; the
 * routes (/backtests/[id], /strategies/[id]) load the live data.
 *
 * If these rows are ever lost from the production DB the cards will
 * 404 — Phase 5.2b deployment must either preserve them via a data
 * migration or re-run the three analyses against the production DB.
 */
const RECEIPTS = {
  bollinger: {
    strategyId: "1facd855-f995-4b73-85b2-a0a789a38044",
    backtestId: "51bf13e7-5f0e-4d43-907e-9000f203d208",
  },
  goldenCross: {
    strategyId: "8045af6b-80db-454e-8769-177171ea5070",
    backtestId: "f9a273f4-1149-4743-9336-2f1e2b0f5e56",
  },
  dungeon: {
    strategyId: "ce2ed293-e140-4d1d-96d9-d5a68fb0536b",
  },
} as const;

export default function LandingPage(): React.ReactElement {
  return (
    <main className="mx-auto w-full max-w-[1100px] px-6 py-10 sm:px-10">
      <PageHeader />

      <Hero />

      <Separator className="my-20" />

      <ReceiptsSection />

      <Separator className="my-20" />

      <HowItWorksSection />

      <Separator className="my-20" />

      <PointOfViewSection />

      <FooterBand />
    </main>
  );
}

// --- Header -----------------------------------------------------------------

function PageHeader(): React.ReactElement {
  // The chrome (theme toggle + ⌘K palette trigger) lives in the
  // global NavRail mounted by the root layout. This header is
  // just the brand eyebrow.
  return (
    <header className="flex items-center justify-between border-b border-hairline pb-4">
      <span className="eyebrow">MarketMind</span>
    </header>
  );
}

// --- Hero -------------------------------------------------------------------

function Hero(): React.ReactElement {
  return (
    <section className="pt-20 sm:pt-24">
      <h1 className="max-w-[18ch] font-serif text-3xl text-ink sm:text-4xl">
        Honest analysis of the trading strategies on YouTube.
      </h1>
      <p className="mt-8 max-w-prose font-serif text-lg leading-relaxed text-muted">
        Paste any strategy video. We extract the rules, run a backtest, compare to buy-and-hold, and
        score it for overfitting. No paid course included.
      </p>
      {/* Short hairline anchors the CTA visually so it doesn't feel
          orphaned beneath a 4xl headline. Editorial-style section
          delimiter, not decoration. */}
      <Separator className="mt-10 w-12" />
      <div className="mt-8 flex flex-wrap items-baseline gap-x-6 gap-y-3">
        <Button asChild intent="primary" size="lg">
          <Link href="/extract">Analyse a strategy →</Link>
        </Button>
        <span className="text-xs text-muted">
          Crypto strategies only in v1. ~2–5 minutes per analysis. Free.
        </span>
      </div>
    </section>
  );
}

// --- Receipts ---------------------------------------------------------------

function ReceiptsSection(): React.ReactElement {
  return (
    <section>
      <SectionHeading eyebrow="Receipts" title="What honest analysis looks like." />

      <div className="mt-12 flex flex-col">
        <ReceiptCard
          eyebrow="Mean reversion on BTC"
          finding="Underperformed buy-and-hold by 908.50%."
          metrics={[
            { label: "Strategy", value: "+87.73%", tone: "neutral" },
            { label: "Buy & hold", value: "+996.23%", tone: "neutral" },
            { label: "Alpha", value: "−908.50%", tone: "negative" },
          ]}
          body={
            <>
              A widely-taught Bollinger Bands + RSI mean reversion strategy. 73% win rate, 3.4
              profit factor, low drawdown — and lost to holding Bitcoin by an order of magnitude.
              The strategy triggered only on extreme oversold readings, missing most of the bull
              market.
            </>
          }
          source={`YouTube · backtest ${RECEIPTS.bollinger.backtestId.slice(0, 8)}`}
          href={`/backtests/${RECEIPTS.bollinger.backtestId}`}
        />

        <Separator className="my-12" />

        <ReceiptCard
          eyebrow="Golden Cross on BTC"
          finding="Real edge, but the marketing is fictional."
          metrics={[
            { label: "Strategy", value: "+684.62%", tone: "neutral" },
            { label: "Buy & hold", value: "+672.59%", tone: "neutral" },
            { label: "Alpha", value: "+12.03%", tone: "positive" },
          ]}
          extraStat={
            <>
              <span className="num">Author claimed: 6,200%</span>
              <span className="mx-3 text-hairline">·</span>
              <span className="num">Overfitting score: 47/100</span>
            </>
          }
          body={
            <>
              The 50/200 SMA Golden Cross strategy outperformed buy-and-hold by 12pp with half the
              drawdown — a real, modest risk-adjusted edge. But the YouTuber&rsquo;s claimed 6,200%
              return came from a 4-coin portfolio with optimised parameters. The deflated Sharpe
              ratio (accounting for selection bias) reduces the probability of a real edge to near
              zero.
            </>
          }
          source={`YouTube · backtest ${RECEIPTS.goldenCross.backtestId.slice(0, 8)}`}
          href={`/backtests/${RECEIPTS.goldenCross.backtestId}`}
        />

        <Separator className="my-12" />

        <ReceiptCard
          eyebrow="Proprietary indicator"
          finding="Refused — strategy depends on subjective judgment."
          subFinding="Not all strategies can be honestly backtested. We say so."
          statusLine="STATUS: REFUSED"
          body={
            <>
              This video promised 11,300% in 12 months using a proprietary &ldquo;Dungeon
              Channels&rdquo; indicator and manually-drawn support / resistance levels. Both make
              the strategy impossible to mechanically test. Rather than guess or fabricate a
              comparison, we refused with the reasoning cited.
            </>
          }
          source={`YouTube · strategy ${RECEIPTS.dungeon.strategyId.slice(0, 8)}`}
          href={`/strategies/${RECEIPTS.dungeon.strategyId}`}
          linkLabel="Read the refusal →"
        />
      </div>
    </section>
  );
}

interface Metric {
  label: string;
  value: string;
  tone: "neutral" | "positive" | "negative";
}

interface ReceiptCardProps {
  eyebrow: string;
  finding: string;
  subFinding?: string;
  metrics?: ReadonlyArray<Metric>;
  // statusLine replaces the metric row when a card doesn't have numbers
  // to show (e.g. the refusal case) — a single mono accent line so the
  // right column doesn't read as empty next to a populated header.
  statusLine?: string;
  extraStat?: React.ReactNode;
  body: React.ReactNode;
  source: string;
  href: string;
  linkLabel?: string;
}

function ReceiptCard({
  eyebrow,
  finding,
  subFinding,
  metrics,
  statusLine,
  extraStat,
  body,
  source,
  href,
  linkLabel = "Read the full analysis →",
}: ReceiptCardProps): React.ReactElement {
  return (
    <article className="grid grid-cols-1 gap-8 md:grid-cols-12">
      <header className="md:col-span-4">
        <p className="eyebrow">{eyebrow}</p>
        <h3 className="mt-4 font-serif text-2xl text-ink">{finding}</h3>
        {subFinding ? <p className="mt-3 text-sm text-muted">{subFinding}</p> : null}
      </header>

      <div className="md:col-span-8">
        {metrics ? (
          <dl className="mb-6 grid grid-cols-3 gap-x-6 border-y border-hairline py-5">
            {metrics.map((m) => (
              <div key={m.label}>
                <dt className="eyebrow">{m.label}</dt>
                <dd
                  className={
                    "mt-2 font-mono text-xl tabular-nums " +
                    (m.tone === "positive"
                      ? "text-positive"
                      : m.tone === "negative"
                        ? "text-negative"
                        : "text-ink")
                  }
                >
                  {m.value}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}

        {statusLine ? (
          <p className="mb-6 border-y border-hairline py-5 font-mono text-sm tracking-wide text-accent tabular-nums">
            {statusLine}
          </p>
        ) : null}

        {extraStat ? (
          <p className="mb-5 font-mono text-xs text-muted tabular-nums">{extraStat}</p>
        ) : null}

        <p className="max-w-prose font-sans text-base leading-relaxed text-ink">{body}</p>

        <div className="mt-6 flex flex-wrap items-baseline gap-x-6 gap-y-2">
          <Link
            href={href}
            className="text-sm text-ink underline decoration-muted decoration-1 underline-offset-4 transition-colors hover:decoration-ink"
          >
            {linkLabel}
          </Link>
          <span className="font-mono text-xs text-muted tabular-nums">Source: {source}</span>
        </div>
      </div>
    </article>
  );
}

// --- How it works -----------------------------------------------------------

function HowItWorksSection(): React.ReactElement {
  // Mirrors the receipts grid: header in the left 4 columns, the
  // step row in the right 8. Keeps a single editorial rhythm running
  // top-to-bottom instead of switching layout per section.
  return (
    <section className="grid grid-cols-1 gap-8 md:grid-cols-12">
      <header className="md:col-span-4">
        <p className="eyebrow">Process</p>
        <h2 className="mt-4 max-w-[20ch] font-serif text-2xl text-ink sm:text-3xl">
          Three steps. About two minutes.
        </h2>
      </header>

      <ol className="grid grid-cols-1 gap-10 md:col-span-8 md:grid-cols-3">
        <Step
          number="01"
          title="Submit a URL"
          body="Paste any YouTube trading-strategy video. Audio transcribed automatically."
        />
        <Step
          number="02"
          title="AI extracts the strategy"
          body="An LLM parses the rules into a strict schema, or refuses if the strategy isn't mechanically testable."
        />
        <Step
          number="03"
          title="We backtest and score it"
          body="Six years of real Binance OHLCV, no look-ahead bias, realistic costs, benchmarked against buy-and-hold, scored for overfitting."
        />
      </ol>
    </section>
  );
}

function Step({
  number,
  title,
  body,
}: {
  number: string;
  title: string;
  body: string;
}): React.ReactElement {
  return (
    <li className="flex flex-col">
      <span className="font-mono text-xs tabular-nums text-muted">{number}</span>
      <h3 className="mt-3 font-sans text-base font-semibold text-ink">{title}</h3>
      <p className="mt-3 text-sm leading-relaxed text-muted">{body}</p>
    </li>
  );
}

// --- Point of view ----------------------------------------------------------

function PointOfViewSection(): React.ReactElement {
  return (
    <section>
      <SectionHeading eyebrow="Point of view" title="Why this isn’t another backtester." />

      <div className="mt-12 flex max-w-prose flex-col gap-10">
        <Stanza
          heading="No paid course."
          body="We’re not affiliated with any trader, channel, or strategy provider. We have no incentive to make any strategy look good."
        />
        <Stanza
          heading="Honest refusal."
          body="Most strategies on YouTube depend on subjective judgment, proprietary indicators, or manually-drawn levels. We refuse to fake a backtest for these — and explain why."
        />
        <Stanza
          heading="Benchmarked against doing nothing."
          body="Every backtest shows you what passively holding the asset would have done over the same period. Many widely-taught strategies underperform passive holding by a wide margin. We surface this prominently, not in a footnote."
        />
      </div>
    </section>
  );
}

function Stanza({ heading, body }: { heading: string; body: string }): React.ReactElement {
  return (
    <div>
      <h3 className="font-serif text-xl text-ink">{heading}</h3>
      <p className="mt-3 font-sans text-base leading-relaxed text-muted">{body}</p>
    </div>
  );
}

// --- Footer -----------------------------------------------------------------

function FooterBand(): React.ReactElement {
  return (
    <footer className="mt-24 border-t border-hairline pt-10">
      <div className="grid grid-cols-1 gap-10 md:grid-cols-12">
        <div className="md:col-span-4">
          <p className="eyebrow">MarketMind</p>
          <p className="mt-3 text-sm leading-relaxed text-muted">
            A research tool that tells you whether trading strategies actually work.
          </p>
        </div>
        <nav className="md:col-span-3">
          <p className="eyebrow">Links</p>
          <ul className="mt-3 flex flex-col gap-2 text-sm">
            <li>
              <Link href="/extract" className="text-ink hover:text-accent">
                Analyse a strategy
              </Link>
            </li>
            <li>
              {/* Trader dashboard is reachable via the ⌘K palette
                  + the breadcrumb on every other page; not surfaced
                  here so the landing footer stays focused on the
                  marketing entry point. */}
              <a
                href="https://github.com/"
                className="text-ink hover:text-accent"
                rel="noopener noreferrer"
                target="_blank"
              >
                GitHub
              </a>
            </li>
            <li>
              <a href="mailto:hello@marketmind.example" className="text-ink hover:text-accent">
                Contact
              </a>
            </li>
          </ul>
        </nav>
        {/* Wider column (col-span-5) so the disclaimer wraps to three
            clean lines instead of the four-line ragged shape we had
            when the three columns were equal width. */}
        <p className="text-xs leading-snug text-muted md:col-span-5">
          Past performance does not predict future results. Backtests are not financial advice.
          Educational tool only.
        </p>
      </div>

      <div className="mt-10 flex items-center justify-between border-t border-hairline pt-5">
        <span className="font-mono text-xs tabular-nums text-muted">
          © {new Date().getFullYear()} MarketMind
        </span>
        {/* ThemeToggle moved to the global NavRail. */}
      </div>
    </footer>
  );
}

// --- shared -----------------------------------------------------------------

function SectionHeading({
  eyebrow,
  title,
}: {
  eyebrow: string;
  title: string;
}): React.ReactElement {
  return (
    <div>
      <p className="eyebrow">{eyebrow}</p>
      <h2 className="mt-4 max-w-[20ch] font-serif text-2xl text-ink sm:text-3xl">{title}</h2>
    </div>
  );
}
