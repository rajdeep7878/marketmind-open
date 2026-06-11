import type { Config } from "tailwindcss";

/**
 * Editorial Quant design tokens.
 *
 * The palette + type scale are LOCKED. Do not extend with additional
 * colours or fonts. Future polish lives in spacing, layout, motion —
 * the foundation stays put.
 *
 * Inspirations: FT data journalism, NYT Upshot, Stripe Press,
 * Pudding.cool. Anti-inspirations: Linear, Bloomberg Terminal, any
 * SaaS marketing site from 2022.
 */
const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  // Class-based dark mode — the .dark class on <html> activates the
  // Honest Terminal palette. Set explicitly by the ThemeToggle +
  // FOWT-prevention script in app/layout.tsx (no `media` strategy
  // because we want explicit user control + persistence).
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "var(--color-bg)",
        surface: "var(--color-surface)",
        ink: "var(--color-ink)",
        muted: "var(--color-muted)",
        hairline: "var(--color-hairline)",
        accent: "var(--color-accent)",
        positive: "var(--color-positive)",
        negative: "var(--color-negative)",
        fill: "var(--color-fill)",
      },
      fontFamily: {
        serif: ["var(--font-source-serif-4)", "Georgia", "Cambria", "Times New Roman", "serif"],
        sans: ["var(--font-ibm-plex-sans)", "Helvetica Neue", "Helvetica", "Arial", "sans-serif"],
        mono: ["var(--font-ibm-plex-mono)", "Menlo", "Consolas", "Liberation Mono", "monospace"],
      },
      fontSize: {
        xs: ["0.75rem", { lineHeight: "1.1rem" }],
        sm: ["0.875rem", { lineHeight: "1.35rem" }],
        base: ["1rem", { lineHeight: "1.6rem" }],
        lg: ["1.125rem", { lineHeight: "1.7rem" }],
        xl: ["1.5rem", { lineHeight: "2rem" }],
        "2xl": ["2rem", { lineHeight: "2.4rem" }],
        "3xl": ["2.5rem", { lineHeight: "2.85rem" }],
        "4xl": ["3.5rem", { lineHeight: "3.8rem" }],
      },
      borderRadius: {
        none: "0",
        DEFAULT: "2px",
        sm: "2px",
        md: "4px",
      },
      borderColor: {
        DEFAULT: "var(--color-hairline)",
      },
      letterSpacing: {
        eyebrow: "0.18em",
      },
      maxWidth: {
        prose: "65ch",
        editorial: "72rem",
      },
    },
  },
  plugins: [],
};

export default config;
