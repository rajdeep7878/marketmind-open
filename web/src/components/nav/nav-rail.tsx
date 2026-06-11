"use client";

import { ThemeToggle } from "@/components/theme-toggle";
import { TriggerPill } from "@/components/nav/trigger-pill";

/**
 * Top-right persistent navigation chrome. Pinned with
 * `fixed top-6 right-6` on every page. Z-index 40 — above page
 * content, below the palette modal (z-50).
 *
 * Composes:
 *   - <TriggerPill /> — opens the command palette (hidden below 640px)
 *   - <ThemeToggle /> — the existing component, unchanged. Visible
 *     at all viewports.
 *
 * The rail is the ONLY place these two controls render. Per-page
 * PageHeader components used to render <ThemeToggle /> inline;
 * those renders have been removed during integration.
 */
interface NavRailProps {
  onOpenPalette: () => void;
}

export function NavRail({ onOpenPalette }: NavRailProps): React.ReactElement {
  return (
    <div className="pointer-events-none fixed right-6 top-6 z-40 flex items-center gap-2">
      <div className="pointer-events-auto">
        <TriggerPill onOpen={onOpenPalette} />
      </div>
      <div className="pointer-events-auto">
        <ThemeToggle />
      </div>
    </div>
  );
}
