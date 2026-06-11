import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Breadcrumbs } from "@/components/nav/breadcrumbs";

describe("Breadcrumbs", () => {
  it("renders nothing when items is empty (home-page convention)", () => {
    const { container } = render(<Breadcrumbs items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("always prepends a 'MarketMind' link to /", () => {
    render(<Breadcrumbs items={[{ label: "Trader" }]} />);
    const root = screen.getByRole("link", { name: "MarketMind" });
    expect(root).toHaveAttribute("href", "/");
  });

  it("renders the final segment as non-linked + aria-current=page", () => {
    render(<Breadcrumbs items={[{ label: "Trader" }]} />);
    const current = screen.getByText("Trader");
    expect(current.tagName).toBe("SPAN");
    expect(current).toHaveAttribute("aria-current", "page");
  });

  it("renders intermediate segments with href as links", () => {
    render(
      <Breadcrumbs
        items={[
          { label: "Admin", href: "/admin/stats" },
          { label: "Stats" },
        ]}
      />,
    );
    const admin = screen.getByRole("link", { name: "Admin" });
    expect(admin).toHaveAttribute("href", "/admin/stats");
  });

  it("renders intermediate segments without href as muted spans (not links)", () => {
    render(
      <Breadcrumbs
        items={[
          { label: "Strategies" },
          { label: "BTC Mean Reversion" },
        ]}
      />,
    );
    // Two non-link segments + the linked "MarketMind" root.
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]!.textContent).toBe("MarketMind");

    // Intermediate "Strategies" segment exists as text but isn't a link.
    expect(screen.getByText("Strategies").tagName).toBe("SPAN");
  });

  it("uses an accessible nav element with a 'Breadcrumb' label", () => {
    render(<Breadcrumbs items={[{ label: "Trader" }]} />);
    expect(screen.getByRole("navigation", { name: "Breadcrumb" })).toBeInTheDocument();
  });
});
