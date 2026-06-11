"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * Theme-aware colour resolver for charts.
 *
 * Recharts props want literal hex strings — they don't expand
 * `var(--color-ink)` themselves. So we read the computed value of
 * each CSS variable from the document root and re-read on every
 * theme switch (we watch `.dark` toggles via a MutationObserver on
 * <html> classList).
 *
 * Server render: returns the light-theme defaults so SSR HTML matches
 * the FOWT-prevention script's first-paint values when the user
 * hasn't picked dark. The client effect then overwrites with the
 * actual computed value on mount.
 */

export interface ThemeColors {
  bg: string;
  surface: string;
  ink: string;
  muted: string;
  hairline: string;
  accent: string;
  positive: string;
  negative: string;
  fill: string;
}

const _LIGHT_DEFAULTS: ThemeColors = {
  bg: "#faf8f3",
  surface: "#ffffff",
  ink: "#1a1815",
  muted: "#5c5852",
  hairline: "#e5e1d8",
  accent: "#8b3a1f",
  positive: "#2d5a3d",
  negative: "#8b2c2c",
  fill: "#f0ebe0",
};

const _CSS_VAR_KEYS: Record<keyof ThemeColors, string> = {
  bg: "--color-bg",
  surface: "--color-surface",
  ink: "--color-ink",
  muted: "--color-muted",
  hairline: "--color-hairline",
  accent: "--color-accent",
  positive: "--color-positive",
  negative: "--color-negative",
  fill: "--color-fill",
};

function readFromDocument(): ThemeColors {
  if (typeof window === "undefined") return _LIGHT_DEFAULTS;
  const styles = window.getComputedStyle(document.documentElement);
  const out = { ..._LIGHT_DEFAULTS };
  (Object.keys(_CSS_VAR_KEYS) as (keyof ThemeColors)[]).forEach((k) => {
    const raw = styles.getPropertyValue(_CSS_VAR_KEYS[k]).trim();
    if (raw) out[k] = raw;
  });
  return out;
}

export function useThemeColors(): ThemeColors {
  const [colors, setColors] = useState<ThemeColors>(_LIGHT_DEFAULTS);

  const refresh = useCallback(() => {
    setColors(readFromDocument());
  }, []);

  useEffect(() => {
    refresh();
    // Watch <html> classList for .dark toggle. cheaper than listening
    // to the toggle component directly — any path that flips the
    // class (toggle, FOWT script late-fire, devtools) re-resolves.
    const observer = new MutationObserver(refresh);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => observer.disconnect();
  }, [refresh]);

  return colors;
}
