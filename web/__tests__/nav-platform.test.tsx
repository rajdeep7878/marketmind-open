import { afterEach, describe, expect, it, vi } from "vitest";

import { modifierKey } from "@/lib/use-platform";

describe("modifierKey", () => {
  it("returns ⌘ on Mac", () => {
    expect(modifierKey("mac")).toBe("⌘");
  });

  it("returns Ctrl elsewhere", () => {
    expect(modifierKey("other")).toBe("Ctrl");
  });
});

describe("usePlatform", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns 'mac' on macOS user-agent", async () => {
    vi.stubGlobal("navigator", { ...navigator, platform: "MacIntel" });
    const { renderHook } = await import("@testing-library/react");
    const { usePlatform } = await import("@/lib/use-platform");
    const { result } = renderHook(() => usePlatform());
    // Hook flips after the effect runs — re-render asserts the
    // mounted value, not the SSR fallback.
    expect(result.current).toBe("mac");
  });

  it("returns 'mac' on iOS user-agent", async () => {
    vi.stubGlobal("navigator", { ...navigator, platform: "iPhone" });
    const { renderHook } = await import("@testing-library/react");
    const { usePlatform } = await import("@/lib/use-platform");
    const { result } = renderHook(() => usePlatform());
    expect(result.current).toBe("mac");
  });

  it("returns 'other' on Linux user-agent", async () => {
    vi.stubGlobal("navigator", { ...navigator, platform: "Linux x86_64" });
    const { renderHook } = await import("@testing-library/react");
    const { usePlatform } = await import("@/lib/use-platform");
    const { result } = renderHook(() => usePlatform());
    expect(result.current).toBe("other");
  });

  it("returns 'other' on Windows user-agent", async () => {
    vi.stubGlobal("navigator", { ...navigator, platform: "Win32" });
    const { renderHook } = await import("@testing-library/react");
    const { usePlatform } = await import("@/lib/use-platform");
    const { result } = renderHook(() => usePlatform());
    expect(result.current).toBe("other");
  });
});
