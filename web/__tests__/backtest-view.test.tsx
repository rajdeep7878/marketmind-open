import { beforeAll, describe, expect, it } from "vitest";

beforeAll(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
});

import type { BacktestResult } from "@/lib/backtest";

function makeResult(overrides: Partial<BacktestResult> = {}): BacktestResult {
  return {
    schema_version: "1.0",
    spec_snapshot: { name: "Test strategy" },
    run: {
      spec_name: "Test strategy",
      meta: {
        symbol: "BTC/USDT",
        primary_timeframe: "1d",
        filter_timeframe: null,
        start: "2024-01-01T00:00:00+00:00",
        end: "2024-12-31T00:00:00+00:00",
        initial_capital: 10000,
        direction: "long",
        defaulted_costs: true,
        defaulted_position_sizing: true,
      },
      equity_curve: [
        { timestamp: "2024-01-01T00:00:00+00:00", value: 10000 },
        { timestamp: "2024-06-01T00:00:00+00:00", value: 11000 },
        { timestamp: "2024-12-31T00:00:00+00:00", value: 12000 },
      ],
      trades: [],
    },
    metrics: {
      total_return_pct: 0.2,
      cagr: 0.2,
      annualized_volatility: 0.3,
      sharpe_ratio: 1.1,
      sortino_ratio: 1.3,
      max_drawdown_pct: 0.1,
      max_drawdown_duration_days: 30,
      calmar_ratio: 2.0,
      num_trades: 5,
      win_rate: 0.6,
      profit_factor: 1.5,
      profit_factor_capped: false,
      avg_win_pct: 0.05,
      avg_loss_pct: -0.03,
      expectancy: 0.01,
      largest_win_pct: 0.1,
      largest_loss_pct: -0.07,
      longest_winning_streak: 2,
      longest_losing_streak: 1,
      avg_trade_duration_days: 30.0,
      exposure_pct: 0.6,
      bars_processed: 365,
      bars_per_year: 365,
    },
    benchmark: {
      total_return_pct: 0.1,
      cagr: 0.1,
      max_drawdown_pct: 0.15,
      sharpe_ratio: 0.5,
      final_value: 11000,
      initial_value: 10000,
      equity_curve: [
        { timestamp: "2024-01-01T00:00:00+00:00", value: 10000 },
        { timestamp: "2024-06-01T00:00:00+00:00", value: 10500 },
        { timestamp: "2024-12-31T00:00:00+00:00", value: 11000 },
      ],
    },
    benchmark_comparison: {
      strategy_return_pct: 0.2,
      benchmark_return_pct: 0.1,
      alpha_pct: 0.1,
      beat_benchmark: true,
      strategy_sharpe: 1.1,
      benchmark_sharpe: 0.5,
      risk_adjusted_alpha: 0.6,
      verdict: "The strategy outperformed buy-and-hold by 10.00%.",
    },
    author_claim_comparisons: [],
    data_fetch_seconds: 0.5,
    compute_seconds: 0.8,
    ...overrides,
  };
}

describe("BacktestView", () => {
  it("renders the green callout and the headline metrics", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { BacktestView } = await import("@/app/backtests/[backtestId]/view");

    render(<BacktestView result={makeResult()} backtestId="bt-1" />);

    expect(screen.getByText(/beat buy-and-hold/i)).toBeInTheDocument();
    expect(screen.getByText(/outperformed buy-and-hold/i)).toBeInTheDocument();
    expect(screen.getByText("BTC/USDT", { exact: false })).toBeInTheDocument();
    // Total-return value renders in the headline + comparison + metric blocks.
    expect(screen.getAllByText("+20.00%").length).toBeGreaterThan(0);
    expect(screen.getByText("Trades (0)")).toBeInTheDocument();
  });

  it("renders the red callout when underperforming", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { BacktestView } = await import("@/app/backtests/[backtestId]/view");

    const result = makeResult({
      benchmark_comparison: {
        strategy_return_pct: 0.05,
        benchmark_return_pct: 0.2,
        alpha_pct: -0.15,
        beat_benchmark: false,
        strategy_sharpe: 0.3,
        benchmark_sharpe: 0.9,
        risk_adjusted_alpha: -0.6,
        verdict:
          "The strategy underperformed buy-and-hold by 15.00%. On total return, a passive hold would have beaten this strategy on this data window — that is the honest result.",
      },
    });
    render(<BacktestView result={result} backtestId="bt-2" />);
    // Appears twice: the badge label and the verdict sentence.
    expect(screen.getAllByText(/underperformed buy-and-hold/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/honest result/i)).toBeInTheDocument();
  });

  it("renders the author-claims table when claims are present", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { BacktestView } = await import("@/app/backtests/[backtestId]/view");

    const result = makeResult({
      author_claim_comparisons: [
        {
          claim_type: "return",
          author_value_raw: "200%",
          author_value_parsed: 2.0,
          measured_value: 0.2,
          measured_label: "total return",
          difference: -1.8,
          discrepancy_ratio: -0.9,
          explanation: "Author claimed total return of 200.00%; we measured 20.00%.",
        },
      ],
    });
    render(<BacktestView result={result} backtestId="bt-3" />);
    expect(screen.getByText(/author claims vs measured/i)).toBeInTheDocument();
    expect(screen.getByText("200%")).toBeInTheDocument();
    expect(screen.getByText(/return of 200.00%; we measured 20.00%/)).toBeInTheDocument();
  });
});
