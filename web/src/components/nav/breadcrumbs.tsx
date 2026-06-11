"use client";

import Link from "next/link";

import { cn } from "@/lib/utils";

/**
 * Breadcrumb trail eyebrow used at the top of every non-home page.
 *
 * `items` is the trail AFTER the "MarketMind" root — the component
 * always prepends that segment so callers never need to. The last
 * item is rendered as plain ink-coloured text (it's the current
 * page); preceding items render as muted Links.
 *
 * Format: `MarketMind / Section / Current page`
 *
 * Each non-final item may optionally carry an `href` — if absent,
 * we render it as muted text (used for intermediate categories
 * like "Strategies" that don't have a list page yet).
 */
export interface BreadcrumbItem {
  label: string;
  href?: string;
}

interface BreadcrumbsProps {
  items: ReadonlyArray<BreadcrumbItem>;
  className?: string;
}

export function Breadcrumbs({
  items,
  className,
}: BreadcrumbsProps): React.ReactElement | null {
  // Pages that pass `items={[]}` get nothing rendered. Used by the
  // landing page so the chrome stays uncluttered there.
  if (items.length === 0) return null;

  const lastIndex = items.length - 1;

  return (
    <nav
      aria-label="Breadcrumb"
      className={cn(
        "flex flex-wrap items-baseline gap-1 text-xs leading-snug",
        className,
      )}
    >
      <Link
        href="/"
        className="text-muted underline decoration-transparent decoration-1 underline-offset-4 transition-colors hover:text-ink hover:decoration-hairline"
      >
        MarketMind
      </Link>
      {items.map((item, i) => {
        const isCurrent = i === lastIndex;
        return (
          <span key={`${i}-${item.label}`} className="flex items-baseline gap-1">
            <Separator />
            {isCurrent || !item.href ? (
              <span
                aria-current={isCurrent ? "page" : undefined}
                className={cn(
                  "transition-colors",
                  isCurrent ? "text-ink" : "text-muted",
                )}
              >
                {item.label}
              </span>
            ) : (
              <Link
                href={item.href}
                className="text-muted underline decoration-transparent decoration-1 underline-offset-4 transition-colors hover:text-ink hover:decoration-hairline"
              >
                {item.label}
              </Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}

function Separator(): React.ReactElement {
  return (
    <span aria-hidden className="select-none text-hairline">
      /
    </span>
  );
}
