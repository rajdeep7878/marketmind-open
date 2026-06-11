import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Card primitive. Hairline border, no shadow, near-zero radius.
 * Background defaults to surface; pass `surface=false` to render
 * directly on the page bg.
 */
function Card({
  className,
  surface = true,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { surface?: boolean }): React.ReactElement {
  return (
    <div
      className={cn(
        "rounded-sm border border-hairline p-6",
        surface ? "bg-surface" : "bg-transparent",
        className,
      )}
      {...props}
    />
  );
}

function CardHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.ReactElement {
  return <div className={cn("mb-4 flex flex-col gap-1", className)} {...props} />;
}

function CardTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>): React.ReactElement {
  return <h3 className={cn("font-serif text-xl text-ink", className)} {...props} />;
}

function CardEyebrow({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.ReactElement {
  return <div className={cn("eyebrow", className)} {...props} />;
}

function CardContent({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.ReactElement {
  return <div className={cn("text-sm text-ink", className)} {...props} />;
}

export { Card, CardContent, CardEyebrow, CardHeader, CardTitle };
