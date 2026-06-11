import { describe, it, expect, beforeAll } from "vitest";

beforeAll(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
});

import type { ExtractionResult } from "@/lib/extraction";

function makeReport(
  overrides: Partial<ExtractionResult["report"]> = {},
): ExtractionResult["report"] {
  return {
    verdict: "fully_extractable",
    overall_confidence: 0.85,
    summary: "Test summary",
    extracted_rules: [],
    backtestable_parts: [],
    non_backtestable_parts: [],
    author_claims: [],
    reasoning: "Test reasoning",
    refusal_explanation: null,
    ...overrides,
  };
}

describe("StrategyView", () => {
  it("renders the green badge and spec JSON for fully_extractable", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { StrategyView } = await import("@/app/strategies/[strategyId]/view");

    const result: ExtractionResult = {
      spec: { name: "Test strategy", direction: "long" },
      report: makeReport({
        verdict: "fully_extractable",
        overall_confidence: 0.9,
        extracted_rules: [
          {
            field: "entry",
            value_description: "50 SMA crosses 200 SMA",
            extractable: true,
            confidence: 0.95,
            quote: null,
          },
        ],
        backtestable_parts: ["entry", "exit"],
      }),
    };

    render(<StrategyView result={result} strategyId="abc-123" />);

    expect(screen.getByText(/fully extractable/i)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "90");
    expect(screen.getByText("Test summary")).toBeInTheDocument();
    expect(screen.getByText("Test reasoning")).toBeInTheDocument();
    expect(screen.getByText("50 SMA crosses 200 SMA")).toBeInTheDocument();
    // "Test strategy" now appears in multiple places — the
    // breadcrumb, the h1 title, and the spec JSON block. The
    // breadcrumb + heading were added as part of the unified-
    // navigation work; this assertion only requires the spec
    // name to render somewhere.
    expect(screen.getAllByText(/Test strategy/).length).toBeGreaterThan(0);
  });

  it("renders the red refusal block when spec is null and verdict is not_extractable", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { StrategyView } = await import("@/app/strategies/[strategyId]/view");

    const result: ExtractionResult = {
      spec: null,
      report: makeReport({
        verdict: "not_extractable",
        overall_confidence: 0.05,
        refusal_explanation: "Hand-drawn levels cannot be backtested.",
        non_backtestable_parts: ["entry on subjective S/R"],
      }),
    };

    render(<StrategyView result={result} strategyId="zzz-999" />);

    expect(screen.getByText(/not extractable/i)).toBeInTheDocument();
    expect(screen.getByText(/why we refused/i)).toBeInTheDocument();
    expect(screen.getByText(/Hand-drawn levels cannot be backtested/)).toBeInTheDocument();
    // Spec JSON section should not render
    expect(screen.queryByText(/StrategySpec \(JSON\)/i)).not.toBeInTheDocument();
  });

  it("renders the gray badge for not_a_strategy", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { StrategyView } = await import("@/app/strategies/[strategyId]/view");

    const result: ExtractionResult = {
      spec: null,
      report: makeReport({
        verdict: "not_a_strategy",
        overall_confidence: 0.0,
        refusal_explanation: "Market commentary only.",
      }),
    };

    render(<StrategyView result={result} strategyId="x" />);
    expect(screen.getByText(/not a strategy/i)).toBeInTheDocument();
  });
});
