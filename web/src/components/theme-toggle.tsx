"use client";

/**
 * ThemeToggle — Honest Terminal (dark) vs Editorial Quant (light).
 *
 * Visual: matches the .liquid-glass aesthetic of the trigger pill —
 * a 32×32 circular glass button with the sun/moon icon morphing
 * via a 300 ms rotation + scale crossfade. Both icons render in
 * absolute stacking so neither layout-shifts the other; opacity +
 * transform handles the swap. Reduced-motion users see an instant
 * opacity-only swap (the spring scale + rotation are dropped by
 * the .liquid-glass--interactive @media rule + the inline
 * transition; we explicitly drop transform here too).
 *
 * Behavior preserved from the previous version:
 *   - First-time visitor: hydrate from the .dark class the FOWT-
 *     prevention script applied (see app/layout.tsx + lib/theme-
 *     script.ts).
 *   - Click: flip the .dark class, persist to
 *     localStorage("marketmind-theme").
 *   - SSR / pre-hydration: empty placeholder same size as the
 *     hydrated button so no layout shift.
 *
 * The exported THEME_STORAGE_KEY is used by the command palette's
 * "Toggle theme" action so both code paths agree on the key.
 */

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

export const THEME_STORAGE_KEY = "marketmind-theme";

type Theme = "light" | "dark";

function currentDocumentTheme(): Theme {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function ThemeToggle(): React.ReactElement | null {
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    setTheme(currentDocumentTheme());
  }, []);

  function toggle(): void {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.classList.toggle("dark", next === "dark");
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Private mode / disabled storage — toggle still works for the
      // session, it just doesn't persist.
    }
  }

  if (theme === null) {
    // SSR-safe placeholder — same dimensions as the hydrated button
    // so the nav rail doesn't jump on hydrate.
    return (
      <span
        aria-hidden
        className="inline-block h-8 w-8 rounded-full border border-hairline"
      />
    );
  }

  const isDark = theme === "dark";
  const label = isDark ? "Switch to light theme" : "Switch to dark theme";

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={label}
      title={label}
      className={cn(
        "liquid-glass liquid-glass--interactive",
        "relative isolate inline-flex h-8 w-8 items-center justify-center rounded-full",
      )}
    >
      {/* Icon-morph stack: both icons share the same coordinates;
          we crossfade by toggling opacity + rotation + scale on
          each. 300ms duration feels weighty enough to register as
          a "system" animation, not a flicker.

          The rotation directions are intentionally opposite
          (sun rotates -90 → 0, moon rotates 0 → 90) so the icons
          appear to spin in from different sides — a small detail
          that makes the swap feel more like a substitution and
          less like a fade. */}
      <span className="relative z-10 inline-flex h-4 w-4 items-center justify-center">
        <Sun
          aria-hidden
          strokeWidth={1.6}
          className={cn(
            "absolute h-4 w-4 text-ink",
            "transition-[opacity,transform] duration-300",
            "motion-reduce:transition-opacity motion-reduce:duration-100",
            isDark
              ? "rotate-0 scale-100 opacity-100"
              : "-rotate-90 scale-50 opacity-0 motion-reduce:rotate-0 motion-reduce:scale-100",
          )}
          style={{ transitionTimingFunction: "var(--ease-spring)" }}
        />
        <Moon
          aria-hidden
          strokeWidth={1.6}
          className={cn(
            "absolute h-4 w-4 text-ink",
            "transition-[opacity,transform] duration-300",
            "motion-reduce:transition-opacity motion-reduce:duration-100",
            !isDark
              ? "rotate-0 scale-100 opacity-100"
              : "rotate-90 scale-50 opacity-0 motion-reduce:rotate-0 motion-reduce:scale-100",
          )}
          style={{ transitionTimingFunction: "var(--ease-spring)" }}
        />
      </span>
    </button>
  );
}
