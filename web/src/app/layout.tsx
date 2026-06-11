import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, Source_Serif_4 } from "next/font/google";

import { CommandPaletteProvider } from "@/components/nav/command-palette-provider";
import { PlausibleAnalytics } from "@/components/plausible";
import { themeScript } from "@/lib/theme-script";

import "./globals.css";

// Editorial Quant typography. Source Serif 4 carries display + hero
// numerics; IBM Plex Sans carries body + UI; IBM Plex Mono carries
// every numeric in tables and KPI cards.
const sourceSerif = Source_Serif_4({
  subsets: ["latin"],
  weight: ["400", "700"],
  variable: "--font-source-serif-4",
  display: "swap",
});

const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-ibm-plex-sans",
  display: "swap",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-ibm-plex-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "MarketMind AI",
  description: "Extract strategies from trading content and stress-test them against overfitting.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>): React.ReactElement {
  const fontClasses = [sourceSerif.variable, plexSans.variable, plexMono.variable].join(" ");
  return (
    <html lang="en" className={fontClasses} suppressHydrationWarning>
      <head>
        {/* FOWT-prevention: this script runs before paint, reads
            localStorage / prefers-color-scheme, and applies the .dark
            class synchronously so the first frame matches the user's
            chosen theme. suppressHydrationWarning above tells React
            not to flag the mismatched className it would otherwise see. */}
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        {/* Analytics snippet — renders only when
            NEXT_PUBLIC_PLAUSIBLE_DOMAIN is configured; excludes
            /admin/* via the data-exclude attribute on the script. */}
        <PlausibleAnalytics />
      </head>
      <body className="min-h-screen bg-bg font-sans text-ink antialiased">
        {/* CommandPaletteProvider mounts the persistent NavRail
            (top-right trigger pill + theme toggle) and the ⌘K
            modal. The pill is hidden below sm: viewports; the
            keyboard shortcut still works at every width. */}
        <CommandPaletteProvider>{children}</CommandPaletteProvider>
      </body>
    </html>
  );
}
