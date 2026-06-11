"use client";

import { cn } from "@/lib/utils";

/**
 * Subtle filled circle status indicator. Used in the four status-
 * strip panels and inline next to strategy / activity rows.
 *
 * Colour mapping stays within the locked Editorial Quant + Honest
 * Terminal palette:
 *   green   → positive (forest)
 *   yellow  → accent   (sienna in light / amber in dark — the warm
 *                       warning channel we already have, no new
 *                       token needed)
 *   red     → negative (oxblood)
 *   neutral → muted
 */
type DotLevel = "green" | "yellow" | "red" | "neutral";

interface StatusDotProps {
  level: DotLevel;
  size?: "sm" | "md";
  className?: string;
  "aria-label"?: string;
}

const LEVEL_BG: Record<DotLevel, string> = {
  green: "bg-positive",
  yellow: "bg-accent",
  red: "bg-negative",
  neutral: "bg-muted",
};

export function StatusDot({
  level,
  size = "md",
  className,
  ...rest
}: StatusDotProps): React.ReactElement {
  const dim = size === "sm" ? "h-2 w-2" : "h-2.5 w-2.5";
  return (
    <span
      className={cn("inline-block rounded-full", dim, LEVEL_BG[level], className)}
      role={rest["aria-label"] ? "img" : undefined}
      aria-label={rest["aria-label"]}
    />
  );
}
