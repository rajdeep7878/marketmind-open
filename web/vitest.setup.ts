import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom doesn't ship ResizeObserver, but Recharts' ResponsiveContainer
// constructs one on mount. Provide a noop shim so chart-rendering tests
// don't crash with `ResizeObserver is not defined`.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

// next/navigation hooks require a RouterContext, which @testing-library
// doesn't set up by default. Stub the parts we use so client components
// render under the test runner without a real router wired up. Any
// test that asserts on routing behaviour can override these via
// vi.mocked or vi.spyOn at the test level.
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
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

// Testing-library 16+ ships auto-cleanup only when its globals hook
// runs at module-load. Vitest's `globals: true` config doesn't trigger
// that hook, so we wire it up explicitly here. Without this, the DOM
// from one test bleeds into the next, which surfaces as "elements
// already in the document" failures on otherwise-correct assertions.
afterEach(() => {
  cleanup();
});
