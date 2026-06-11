import { afterEach, describe, expect, it } from "vitest";

describe("PlausibleAnalytics", () => {
  afterEach(() => {
    delete process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN;
    delete process.env.NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL;
  });

  it("renders nothing when NEXT_PUBLIC_PLAUSIBLE_DOMAIN is unset", async () => {
    const { render } = await import("@testing-library/react");
    const { PlausibleAnalytics } = await import("@/components/plausible");
    const { container } = render(<PlausibleAnalytics />);
    expect(container).toBeEmptyDOMElement();
  });

  it("emits a <script> with the configured domain and the default exclusions src", async () => {
    process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN = "marketmind.example";
    const { render, screen } = await import("@testing-library/react");
    const { PlausibleAnalytics } = await import("@/components/plausible");
    render(<PlausibleAnalytics />);
    const script = screen.getByTestId("plausible-script");
    expect(script.getAttribute("data-domain")).toBe("marketmind.example");
    expect(script.getAttribute("data-exclude")).toBe("/admin/*");
    expect(script.getAttribute("src")).toContain("script.exclusions.js");
  });

  it("respects an override script URL", async () => {
    process.env.NEXT_PUBLIC_PLAUSIBLE_DOMAIN = "marketmind.example";
    process.env.NEXT_PUBLIC_PLAUSIBLE_SCRIPT_URL = "https://analytics.example.com/p.js";
    const { render, screen } = await import("@testing-library/react");
    const { PlausibleAnalytics } = await import("@/components/plausible");
    render(<PlausibleAnalytics />);
    const script = screen.getByTestId("plausible-script");
    expect(script.getAttribute("src")).toBe("https://analytics.example.com/p.js");
  });
});
