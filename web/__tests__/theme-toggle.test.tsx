import { afterEach, beforeAll, describe, expect, it } from "vitest";

beforeAll(() => {
  process.env.NEXT_PUBLIC_API_URL = "http://localhost:8000";
});

afterEach(() => {
  document.documentElement.classList.remove("dark");
  window.localStorage.clear();
});

describe("ThemeToggle", () => {
  it("renders a button reflecting the current theme and toggles to the other", async () => {
    const { render, screen, fireEvent } = await import("@testing-library/react");
    const { ThemeToggle, THEME_STORAGE_KEY } = await import("@/components/theme-toggle");

    // Pretend the FOWT script applied dark.
    document.documentElement.classList.add("dark");

    render(<ThemeToggle />);

    // Hydration effect runs after mount; querying immediately should find
    // the button labelled "Switch to light theme" once it appears.
    const button = await screen.findByRole("button", { name: /switch to light theme/i });
    fireEvent.click(button);

    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");

    // Now the button should offer the inverse swap.
    expect(
      await screen.findByRole("button", { name: /switch to dark theme/i }),
    ).toBeInTheDocument();
  });

  it("persists the chosen theme to localStorage", async () => {
    const { render, screen, fireEvent } = await import("@testing-library/react");
    const { ThemeToggle, THEME_STORAGE_KEY } = await import("@/components/theme-toggle");

    document.documentElement.classList.remove("dark");
    render(<ThemeToggle />);

    fireEvent.click(await screen.findByRole("button", { name: /switch to dark theme/i }));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});
