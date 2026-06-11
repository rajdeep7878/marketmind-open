import { beforeAll, describe, expect, it } from "vitest";

beforeAll(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
});

import type { OverfittingAnalysis, OverfittingVerdict } from "@/lib/overfitting";

function makeAnalysis(verdict: OverfittingVerdict, score: number): OverfittingAnalysis {
  return {
    schema_version: "1.0",
    walk_forward: {
      windows: [
        {
          window_index: 0,
          in_sample_start: "2024-01-01T00:00:00+00:00",
          in_sample_end: "2024-04-01T00:00:00+00:00",
          out_of_sample_start: "2024-04-01T00:00:00+00:00",
          out_of_sample_end: "2024-05-01T00:00:00+00:00",
          in_sample_return_pct: 0.1,
          in_sample_sharpe: 1.0,
          in_sample_num_trades: 3,
          out_of_sample_return_pct: 0.05,
          out_of_sample_sharpe: 0.5,
          out_of_sample_num_trades: 1,
        },
      ],
      in_sample_avg_return: 0.1,
      out_of_sample_avg_return: 0.05,
      degradation_ratio: 0.5,
      degradation_ratio_valid: true,
      out_of_sample_positive_rate: 0.5,
      consistency_score: 0.7,
      train_ratio: 0.7,
      n_windows_requested: 6,
      n_windows_actual: 6,
    },
    parameter_sweep: {
      axes: [],
      cells: [],
      baseline_return_pct: 0.1,
      baseline_rank_percentile: 0.6,
      best_in_grid_return: 0.2,
      worst_in_grid_return: 0.0,
      neighborhood_avg_return: 0.08,
      peakiness_score: 0.3,
      n_combinations: 0,
      skipped_reason: null,
    },
    monte_carlo: {
      real_return_pct: 0.1,
      real_sharpe: 1.0,
      n_permutations: 100,
      synthetic_mean_return: 0.0,
      synthetic_std_return: 0.05,
      synthetic_min: -0.2,
      synthetic_max: 0.3,
      histogram: [{ lo: -0.2, hi: 0.3, count: 100 }],
      p_value: 0.1,
      percentile_rank: 0.9,
      seed: 42,
    },
    deflated_sharpe: {
      observed_sharpe: 1.5,
      deflated_sharpe_ratio: 0.5,
      probability_strategy_is_real: 0.7,
      n_trials_estimate: 100,
      n_observations: 1000,
      returns_skewness: 0,
      returns_kurtosis: 3,
      expected_max_sharpe: 1.0,
      method: "lopez_de_prado_full",
    },
    composite: {
      score,
      verdict,
      contributions: [
        {
          name: "walk_forward",
          label: "Walk-forward",
          raw_value: 0.5,
          weight: 0.35,
          contribution_pts: 30,
        },
        {
          name: "parameter_sweep",
          label: "Parameter peakiness",
          raw_value: 0.3,
          weight: 0.25,
          contribution_pts: 20,
        },
        {
          name: "monte_carlo",
          label: "Monte Carlo p-value",
          raw_value: 0.1,
          weight: 0.25,
          contribution_pts: 30,
        },
        {
          name: "deflated_sharpe",
          label: "Deflated Sharpe probability",
          raw_value: 0.7,
          weight: 0.15,
          contribution_pts: 35,
        },
      ],
      explanation: `Verdict: ${verdict} (score ${score}).`,
      confidence_band_low: Math.max(0, score - 10),
      confidence_band_high: Math.min(100, score + 10),
    },
    compute_seconds: 123.4,
  };
}

describe("OverfittingSection", () => {
  it("renders the green Likely Robust headline when score is low", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { OverfittingSection } = await import("@/app/backtests/[backtestId]/overfitting-section");
    render(
      <OverfittingSection backtestId="bt-1" initialAnalysis={makeAnalysis("likely_robust", 22)} />,
    );
    expect(screen.getByText(/Likely Robust/i)).toBeInTheDocument();
    expect(screen.getByText("22")).toBeInTheDocument();
  });

  it("renders the amber Mixed Signals headline mid-range", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { OverfittingSection } = await import("@/app/backtests/[backtestId]/overfitting-section");
    render(
      <OverfittingSection backtestId="bt-2" initialAnalysis={makeAnalysis("mixed_signals", 45)} />,
    );
    expect(screen.getByText(/Mixed Signals/i)).toBeInTheDocument();
    expect(screen.getByText("45")).toBeInTheDocument();
  });

  it("renders the red Likely Overfit headline at the top of the range", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { OverfittingSection } = await import("@/app/backtests/[backtestId]/overfitting-section");
    render(
      <OverfittingSection backtestId="bt-3" initialAnalysis={makeAnalysis("likely_overfit", 78)} />,
    );
    expect(screen.getByText(/Likely Overfit/i)).toBeInTheDocument();
    expect(screen.getByText("78")).toBeInTheDocument();
  });

  it("shows the Run button when no analysis is loaded", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { OverfittingSection } = await import("@/app/backtests/[backtestId]/overfitting-section");
    render(<OverfittingSection backtestId="bt-4" initialAnalysis={null} />);
    expect(screen.getByRole("button", { name: /Run analysis/i })).toBeInTheDocument();
  });
});
