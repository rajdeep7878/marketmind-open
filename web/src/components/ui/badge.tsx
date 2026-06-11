import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Editorial Quant badge. NOT a pill — square 2px corners, hairline
 * border, eyebrow-style type. Used for small status markers (verdict
 * labels in tables, deflated-Sharpe method tag).
 */
const badgeVariants = cva(
  "inline-flex items-center rounded-sm border px-2 py-0.5 font-sans text-[0.6875rem] font-medium uppercase tracking-eyebrow",
  {
    variants: {
      intent: {
        neutral: "border-hairline bg-surface text-muted",
        accent: "border-accent text-accent",
        positive: "border-positive text-positive",
        negative: "border-negative text-negative",
      },
    },
    defaultVariants: { intent: "neutral" },
  },
);

interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, intent, ...props }: BadgeProps): React.ReactElement {
  return <span className={cn(badgeVariants({ intent }), className)} {...props} />;
}

export { Badge, badgeVariants };
