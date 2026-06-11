import { headers } from "next/headers";

import { Breadcrumbs } from "@/components/nav/breadcrumbs";
import { RecordRecentPage } from "@/lib/use-record-recent-page";
import { Separator } from "@/components/ui/separator";
import { apiBaseUrl } from "@/lib/env";

export const dynamic = "force-dynamic";

interface AdminStats {
  generated_at: string;
  submissions: { today: number; week: number; total: number };
  spend: { today_usd: number; week_usd: number; total_usd: number };
  cost_cap: {
    current_usd: number;
    cap_usd: number;
    cap_gbp: number;
    gbp_usd_rate: number;
    fraction_used: number;
  };
  errors_24h_count: number;
  recent_errors: ReadonlyArray<{
    job_id: string;
    ended_at: string | null;
    kind: string | null;
    exception: string;
  }>;
  ratelimit_rejections_today: number;
}

async function fetchStats(): Promise<AdminStats | { error: string }> {
  // The Next.js middleware has already validated the credentials; the
  // same Authorization header is forwarded to the API so the API can
  // re-validate them independently.
  const auth = headers().get("authorization");
  const resp = await fetch(`${apiBaseUrl()}/admin/stats`, {
    headers: auth ? { Authorization: auth } : {},
    cache: "no-store",
  });
  if (!resp.ok) {
    return { error: `API ${resp.status}: ${await resp.text()}` };
  }
  return (await resp.json()) as AdminStats;
}

function fmtUsd(n: number): string {
  return `$${n.toFixed(2)}`;
}

function fmtCapGbp(stats: AdminStats): string {
  const used = stats.cost_cap.current_usd / stats.cost_cap.gbp_usd_rate;
  return `£${used.toFixed(2)} / £${stats.cost_cap.cap_gbp.toFixed(2)}`;
}

export default async function AdminStatsPage(): Promise<React.ReactElement> {
  const result = await fetchStats();

  if ("error" in result) {
    return (
      <main className="mx-auto w-full max-w-[900px] px-6 py-16 sm:px-10">
        <PageHeader />
        <section className="callout mt-12">
          <p className="eyebrow mb-2">Admin / Stats</p>
          <p className="font-serif text-base text-ink">Failed to load stats.</p>
          <pre className="mt-3 whitespace-pre-wrap break-words font-mono text-xs text-muted">
            {result.error}
          </pre>
        </section>
      </main>
    );
  }

  const stats = result;
  return (
    <main className="mx-auto w-full max-w-[900px] px-6 py-16 sm:px-10">
      <RecordRecentPage path="/admin/stats" title="Admin stats" />
      <PageHeader />

      <section className="mt-10">
        <p className="eyebrow">Admin · Stats</p>
        <h1 className="mt-3 font-serif text-3xl text-ink">Operational dashboard</h1>
        <p className="mt-3 font-mono text-xs tabular-nums text-muted">
          Generated at <span className="text-ink">{stats.generated_at}</span>
        </p>
      </section>

      <Separator className="my-10" />

      <StatGroup
        eyebrow="Submissions"
        rows={[
          { label: "Today", value: String(stats.submissions.today) },
          { label: "Past 7 days", value: String(stats.submissions.week) },
          { label: "All-time", value: String(stats.submissions.total) },
        ]}
      />

      <Separator className="my-10" />

      <StatGroup
        eyebrow="Anthropic spend (USD)"
        rows={[
          { label: "Today", value: fmtUsd(stats.spend.today_usd) },
          { label: "Past 7 days", value: fmtUsd(stats.spend.week_usd) },
          { label: "All-time", value: fmtUsd(stats.spend.total_usd) },
        ]}
      />

      <Separator className="my-10" />

      <StatGroup
        eyebrow="Daily cost cap"
        rows={[
          { label: "Used today", value: fmtCapGbp(stats) },
          {
            label: "Fraction",
            value: `${(stats.cost_cap.fraction_used * 100).toFixed(1)}%`,
          },
          {
            label: "GBP→USD rate",
            value: stats.cost_cap.gbp_usd_rate.toFixed(2),
          },
        ]}
      />

      <Separator className="my-10" />

      <StatGroup
        eyebrow="Rate limiting"
        rows={[
          {
            label: "429s today",
            value: String(stats.ratelimit_rejections_today),
          },
        ]}
      />

      <Separator className="my-10" />

      <section>
        <p className="eyebrow">Errors</p>
        <p className="mt-3 font-mono text-sm tabular-nums text-ink">
          {stats.errors_24h_count} job failure(s) in the last 24h.
        </p>
        {stats.recent_errors.length === 0 ? (
          <p className="mt-4 text-sm text-muted">No recent failures recorded.</p>
        ) : (
          <ul className="mt-6 flex flex-col gap-5">
            {stats.recent_errors.map((e) => (
              <li
                key={e.job_id}
                className="border-l-2 border-negative bg-surface px-4 py-3 text-sm"
              >
                <p className="font-mono text-xs tabular-nums text-muted">
                  {e.ended_at ?? "unknown time"} · {e.kind ?? "unknown kind"} ·{" "}
                  <span className="text-ink">{e.job_id}</span>
                </p>
                <p className="mt-2 font-mono text-xs leading-relaxed text-ink">{e.exception}</p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

function PageHeader(): React.ReactElement {
  // The chrome (theme toggle, command palette trigger) now lives
  // in the global NavRail mounted by the root layout. This local
  // header is just the breadcrumb + section eyebrow.
  return (
    <header className="border-b border-hairline pb-4">
      <Breadcrumbs items={[{ label: "Admin" }, { label: "Stats" }]} />
    </header>
  );
}

function StatGroup({
  eyebrow,
  rows,
}: {
  eyebrow: string;
  rows: ReadonlyArray<{ label: string; value: string }>;
}): React.ReactElement {
  return (
    <section>
      <p className="eyebrow">{eyebrow}</p>
      <dl className="mt-4 grid grid-cols-1 gap-x-10 gap-y-4 sm:grid-cols-3">
        {rows.map((row) => (
          <div key={row.label}>
            <dt className="font-sans text-xs text-muted">{row.label}</dt>
            <dd className="mt-1 font-mono text-xl text-ink tabular-nums">{row.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
