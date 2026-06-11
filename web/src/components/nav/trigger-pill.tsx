"use client";

import { Search } from "lucide-react";

import { modifierKey, usePlatform } from "@/lib/use-platform";
import { cn } from "@/lib/utils";

/**
 * The visible "Search · ⌘K" pill that opens the command palette.
 *
 * Visual treatment (Apple liquid-glass aesthetic):
 *   - .liquid-glass + .liquid-glass--interactive picks up the
 *     theme-aware blur/saturate, translucent surface tint, inset
 *     top highlight, soft outer shadow, and spring hover/active
 *     transforms. All of those are theme-aware via the --glass-*
 *     tokens in globals.css.
 *   - Inside the pill, the ⌘K kbd badge gets .liquid-glass--inner
 *     for the "glass inside glass" effect Apple uses on Spotlight
 *     keyboard hints.
 *   - The search icon sits on a tiny inner-glass plate too so the
 *     two icons in the pill (search + kbd) feel like co-equal
 *     glass elements rather than the icon being a bare glyph.
 *
 * Responsive:
 *   - Hidden below 640px (`hidden sm:inline-flex`). The ⌘K /
 *     Ctrl+K keyboard shortcut still works at every width because
 *     its listener lives in the provider, not the pill.
 *   - Platform glyph (⌘ vs Ctrl) auto-detected via navigator.platform.
 */
interface TriggerPillProps {
  onOpen: () => void;
}

export function TriggerPill({ onOpen }: TriggerPillProps): React.ReactElement {
  const platform = usePlatform();
  const kbd = `${modifierKey(platform)}K`;
  return (
    <button
      type="button"
      onClick={onOpen}
      aria-label="Open command palette"
      title={`Open command palette (${kbd})`}
      className={cn(
        "liquid-glass liquid-glass--interactive",
        "relative isolate inline-flex h-8 items-center gap-2 rounded-full px-3",
        "hidden sm:inline-flex",
      )}
    >
      {/* The ::before pseudo-element on .liquid-glass paints the
          vertical light overlay; nothing else needed inline here.
          Children sit above ::before via the default stacking. */}
      <span
        aria-hidden
        className={cn(
          "liquid-glass--inner",
          "relative z-10 inline-flex h-4 w-4 items-center justify-center rounded-full",
        )}
      >
        <Search className="h-2.5 w-2.5 text-muted" strokeWidth={2} />
      </span>
      <span className="relative z-10 text-[12px] font-medium text-ink/85">
        Search
      </span>
      <kbd
        className={cn(
          "liquid-glass--inner",
          "relative z-10 ml-0.5 inline-flex h-5 min-w-[24px] items-center justify-center rounded-[6px] px-1",
          "font-mono text-[10px] font-medium tabular-nums text-muted",
        )}
      >
        {kbd}
      </kbd>
    </button>
  );
}
