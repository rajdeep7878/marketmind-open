"use client";

import { useEffect, useState } from "react";

/**
 * Returns "mac" on macOS / iOS, "other" elsewhere. Hydration-safe:
 * the SSR / first-render value is "other"; the effect overwrites
 * after mount so the UI can render the right keyboard hint
 * (⌘K vs Ctrl+K) without a hydration mismatch warning.
 *
 * navigator.platform is the legacy-but-still-reliable signal here;
 * userAgentData.platform is too sparsely supported as of 2026.
 */
export type Platform = "mac" | "other";

export function usePlatform(): Platform {
  const [platform, setPlatform] = useState<Platform>("other");
  useEffect(() => {
    if (typeof navigator === "undefined") return;
    const raw = navigator.platform ?? "";
    setPlatform(/Mac|iPhone|iPad|iPod/i.test(raw) ? "mac" : "other");
  }, []);
  return platform;
}

/** Convenience: the modifier-key glyph for the current platform. */
export function modifierKey(platform: Platform): string {
  return platform === "mac" ? "⌘" : "Ctrl";
}
