import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Hairline-edged placeholder rectangle. No shimmer — Editorial Quant
 * has near-zero motion; the rectangle is enough to signal "loading"
 * without distracting from the page composition.
 */
function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>): React.ReactElement {
  return <div className={cn("rounded-sm border border-hairline bg-fill", className)} {...props} />;
}

export { Skeleton };
