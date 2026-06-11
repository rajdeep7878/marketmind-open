"use client";

import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * Editorial Quant button. Three intents:
 *
 *   primary   — solid oxblood accent on cream; for the single hero CTA
 *               on a page ("Analyse", "Run backtest")
 *   secondary — hairline-bordered cream surface; for secondary actions
 *   ghost     — type-only link-button for tertiary affordances
 *
 * No box shadows. Border radius is 2px. Hover transitions colour only
 * (no transforms, no scale).
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-sm font-sans text-sm font-medium tracking-tight transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg disabled:pointer-events-none disabled:opacity-40",
  {
    variants: {
      intent: {
        primary: "bg-accent text-bg hover:bg-[color-mix(in_srgb,var(--color-accent)_88%,black)]",
        secondary: "border border-hairline bg-surface text-ink hover:bg-fill",
        ghost:
          "text-ink underline decoration-hairline decoration-1 underline-offset-4 hover:decoration-ink",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-10 px-4",
        lg: "h-12 px-6 text-base",
      },
    },
    defaultVariants: {
      intent: "primary",
      size: "md",
    },
  },
);

interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, intent, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp className={cn(buttonVariants({ intent, size }), className)} ref={ref} {...props} />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
