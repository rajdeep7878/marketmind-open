import { describe, it, expect, beforeAll } from "vitest";

beforeAll(() => {
  // env.ts validates NEXT_PUBLIC_API_URL at module load; provide a valid one.
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
});

describe("landing page", () => {
  it("renders the hero, three receipts, and links to the live analyses", async () => {
    const { render, screen } = await import("@testing-library/react");
    const { default: LandingPage } = await import("@/app/page");

    render(<LandingPage />);

    // Hero headline.
    expect(
      screen.getByText(/Honest analysis of the trading strategies on YouTube\./i),
    ).toBeInTheDocument();

    // Primary CTA points at /extract.
    const ctas = screen.getAllByRole("link", { name: /analyse a strategy/i });
    expect(ctas.length).toBeGreaterThan(0);
    expect(ctas[0]).toHaveAttribute("href", "/extract");

    // Three receipts: two link to /backtests/{id}, one to /strategies/{id}.
    const backtestLinks = screen
      .getAllByRole("link")
      .filter((a) => a.getAttribute("href")?.startsWith("/backtests/"));
    expect(backtestLinks).toHaveLength(2);

    const refusalLink = screen.getByRole("link", { name: /read the refusal/i });
    expect(refusalLink.getAttribute("href")).toMatch(/^\/strategies\//);

    // The three section eyebrows are present (Receipts / Process / Point of view).
    expect(screen.getByText(/Receipts/i)).toBeInTheDocument();
    expect(screen.getByText(/Process/i)).toBeInTheDocument();
    expect(screen.getByText(/Point of view/i)).toBeInTheDocument();
  });
});
