import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CommandPaletteProvider } from "@/components/nav/command-palette-provider";
import { _clearRecentPages } from "@/lib/recent-pages";

// Mocked from vitest.setup.ts; we capture the spy by importing the
// module again here so we can assert on push calls.
const pushSpy = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: pushSpy,
    replace: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
  notFound: () => {
    throw new Error("notFound");
  },
}));

beforeEach(() => {
  pushSpy.mockClear();
  _clearRecentPages();
});

afterEach(() => {
  // The provider's body-scroll lock can stick on test failure. Clear
  // it between tests so the next render starts fresh.
  document.body.style.overflow = "";
});

function Wrapper(): React.ReactElement {
  return (
    <CommandPaletteProvider>
      <button type="button">page content</button>
    </CommandPaletteProvider>
  );
}

describe("CommandPalette open/close behaviour", () => {
  it("opens when the trigger pill is clicked", () => {
    render(<Wrapper />);
    // Pill button labelled "Open command palette".
    fireEvent.click(screen.getByLabelText("Open command palette"));
    // Search input appears.
    expect(screen.getByPlaceholderText(/Search pages/i)).toBeInTheDocument();
  });

  it("opens on ⌘K / Ctrl+K keydown", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.getByPlaceholderText(/Search pages/i)).toBeInTheDocument();
  });

  it("toggles closed on a second ⌘K", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(screen.getByPlaceholderText(/Search pages/i)).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    // The palette starts its close animation; the search input is
    // unmounted after the 200ms timeout. Without a fake-timers
    // setup we just assert it ISN'T still visible after a small
    // wait — but jsdom processes setState synchronously enough
    // that we can check immediately.
    // To keep the test deterministic, assert that the dialog's
    // backdrop fades — easier signal than the unmount timeout.
    // We accept either: input gone OR backdrop opacity=0.
    const input = screen.queryByPlaceholderText(/Search pages/i);
    expect(input?.closest("[aria-modal='true']")?.className ?? "").toContain("opacity-0");
  });

  it("closes on Escape", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByPlaceholderText(/Search pages/i);
    fireEvent.keyDown(input.closest("[role='dialog']")!, { key: "Escape" });
    const inputAfter = screen.queryByPlaceholderText(/Search pages/i);
    expect(inputAfter?.closest("[aria-modal='true']")?.className ?? "").toContain("opacity-0");
  });
});

describe("CommandPalette search + navigation", () => {
  it("filters results as the user types", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByPlaceholderText(/Search pages/i);
    fireEvent.change(input, { target: { value: "trader" } });
    // "Trader dashboard" page result is visible.
    expect(screen.getByText("Trader dashboard")).toBeInTheDocument();
    // Other pages shouldn't match "trader".
    expect(screen.queryByText("Analyse a strategy")).not.toBeInTheDocument();
  });

  it("Enter on a selected result navigates via router.push", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByPlaceholderText(/Search pages/i);
    fireEvent.change(input, { target: { value: "trader" } });
    const dialog = input.closest("[role='dialog']")!;
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(pushSpy).toHaveBeenCalledWith("/trader");
  });

  it("ArrowDown moves selection between results", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByPlaceholderText(/Search pages/i);
    // Empty query — all pages + actions visible.
    const dialog = input.closest("[role='dialog']")!;
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    // The second page-row should be "Analyse a strategy" → /extract.
    expect(pushSpy).toHaveBeenCalledTimes(1);
    expect(pushSpy.mock.calls[0]![0]).toBe("/extract");
  });

  it("ArrowUp from index 0 wraps to last row", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const dialog = screen
      .getByPlaceholderText(/Search pages/i)
      .closest("[role='dialog']")!;
    fireEvent.keyDown(dialog, { key: "ArrowUp" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    // Last row in the flat list is the "Toggle theme" action; it
    // doesn't navigate via router. So push shouldn't have been
    // called. The assertion is just that no crash + push==0.
    expect(pushSpy).toHaveBeenCalledTimes(0);
  });

  it("shows 'No matches' when the query filters out everything", () => {
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    const input = screen.getByPlaceholderText(/Search pages/i);
    fireEvent.change(input, { target: { value: "zzzqqq" } });
    expect(screen.getByText(/No matches/)).toBeInTheDocument();
  });
});

describe("CommandPalette body-scroll lock", () => {
  it("sets body overflow:hidden on open and restores on close", () => {
    document.body.style.overflow = "scroll"; // pre-existing value
    render(<Wrapper />);
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.keyDown(window, { key: "k", metaKey: true });
    expect(document.body.style.overflow).toBe("scroll");
  });
});
