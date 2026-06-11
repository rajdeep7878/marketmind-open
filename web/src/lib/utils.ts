import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Standard shadcn className combinator. Use everywhere a component
 * accepts a `className` override so authoring classes compose cleanly
 * with the design-system defaults.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
