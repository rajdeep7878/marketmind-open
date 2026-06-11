"use client";

import {
  Activity,
  Clock,
  FileSearch,
  Home,
  LayoutDashboard,
  Search,
  SunMoon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { THEME_STORAGE_KEY } from "@/components/theme-toggle";
import { rankByMatch } from "@/lib/fuzzy-match";
import { readRecentPages } from "@/lib/recent-pages";
import { modifierKey, usePlatform } from "@/lib/use-platform";
import { cn } from "@/lib/utils";

/**
 * The ⌘K / Ctrl+K command palette — Spotlight-grade liquid glass.
 *
 * Visual treatment:
 *   - Backdrop: glass-morphism dim layer (backdrop-blur-2xl over a
 *     translucent --color-bg tint). 250 ms ease-out fade.
 *   - Panel: .liquid-glass--panel (40px blur + 180% saturate, 80%
 *     surface tint, inset top highlight, soft outer shadow). Scales
 *     0.94 → 1.0 over 300 ms with var(--ease-spring) — Apple's
 *     subtle-overshoot curve. Opacity fades over 200 ms.
 *   - Search input: recessed inset-shadow look + a small liquid-
 *     glass--inner plate behind the search icon, matching the
 *     trigger pill's "glass inside glass" pattern.
 *   - Section rules (.pane-rule): horizontal gradient that fades
 *     transparent → hairline → transparent.
 *   - Row hover: subtle white-tinted glass highlight.
 *   - Row selected: accent-tinted glass tint (--glass-accent-tint)
 *     plus a soft inset accent glow. Cross-fades on Arrow nav over
 *     150 ms.
 *   - Result rows fade in via @keyframes paletteRowIn with a
 *     per-index animation-delay capped at 240 ms total.
 *
 * Functional behaviour is unchanged — this is purely visual and
 * motion work over the same React structure as before.
 */

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  triggerRef: React.RefObject<HTMLElement>;
}

interface ResultBase {
  key: string;
  title: string;
  subtitle?: string | undefined;
  icon: React.ReactNode;
  kbdHint?: string | undefined;
  execute: () => void;
}

const PAGE_CATALOG: ReadonlyArray<Omit<ResultBase, "execute">> & {
  // Carry the path along separately so we can wire `execute` at
  // render time with the live router.
  [k: number]: Omit<ResultBase, "execute"> & { path: string };
} = [
  {
    key: "page:home",
    title: "Home",
    subtitle: "/",
    icon: <Home className="h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden />,
    path: "/",
  },
  {
    key: "page:extract",
    title: "Analyse a strategy",
    subtitle: "/extract",
    icon: <FileSearch className="h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden />,
    path: "/extract",
  },
  {
    key: "page:trader",
    title: "Trader dashboard",
    subtitle: "/trader",
    icon: <Activity className="h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden />,
    path: "/trader",
  },
  {
    key: "page:admin",
    title: "Admin stats",
    subtitle: "/admin/stats",
    icon: <LayoutDashboard className="h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden />,
    path: "/admin/stats",
  },
] as const as never;

function toggleTheme(): void {
  if (typeof document === "undefined") return;
  const isDark = document.documentElement.classList.contains("dark");
  const next = isDark ? "light" : "dark";
  document.documentElement.classList.toggle("dark", next === "dark");
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    // private browsing — toggle still takes effect for the session.
  }
}

export function CommandPalette({
  open,
  onClose,
  triggerRef,
}: CommandPaletteProps): React.ReactElement | null {
  const router = useRouter();
  const platform = usePlatform();

  // Two-step open: `mounted` controls whether the DOM exists at
  // all; `visible` controls the opacity / scale / transform
  // classes. Flipping `visible` on the next frame triggers the
  // transition.
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);

  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Mount → next-frame visible. Close → invisible → unmount after
  // the longest exit transition (transform=200ms close).
  useEffect(() => {
    if (open) {
      setMounted(true);
      const id = requestAnimationFrame(() => setVisible(true));
      return () => cancelAnimationFrame(id);
    }
    setVisible(false);
    const id = setTimeout(() => setMounted(false), 250);
    return () => clearTimeout(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setSelectedIndex(0);
    const id = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [open]);

  useEffect(() => {
    if (open) return;
    if (!triggerRef.current) return;
    if (document.contains(triggerRef.current)) {
      triggerRef.current.focus({ preventScroll: true });
    }
  }, [open, triggerRef]);

  const sections = useMemo(() => {
    const recent: ResultBase[] = readRecentPages()
      .filter((r) => r.path !== "/")
      .map((r) => ({
        key: `recent:${r.path}`,
        title: r.title,
        subtitle: r.path,
        icon: <Clock className="h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden />,
        execute: () => {
          router.push(r.path);
          onClose();
        },
      }));

    const pages: ResultBase[] = (PAGE_CATALOG as unknown as ReadonlyArray<
      Omit<ResultBase, "execute"> & { path: string }
    >).map((p) => ({
      key: p.key,
      title: p.title,
      subtitle: p.subtitle,
      icon: p.icon,
      execute: () => {
        router.push(p.path);
        onClose();
      },
    }));

    const actions: ResultBase[] = [
      {
        key: "action:theme",
        title: "Toggle theme",
        subtitle: "Switch between dark and light",
        icon: <SunMoon className="h-4 w-4 text-muted" strokeWidth={1.5} aria-hidden />,
        execute: () => {
          toggleTheme();
          onClose();
        },
      },
    ];

    const q = query.trim();
    if (q.length === 0) {
      return { recent, pages, actions };
    }
    return {
      recent: rankByMatch<ResultBase>(q, recent),
      pages: rankByMatch<ResultBase>(q, pages),
      actions: rankByMatch<ResultBase>(q, actions),
    };
  }, [query, router, onClose]);

  const flat: ResultBase[] = useMemo(
    () => [...sections.recent, ...sections.pages, ...sections.actions],
    [sections],
  );

  useEffect(() => {
    if (selectedIndex >= flat.length) {
      setSelectedIndex(Math.max(0, flat.length - 1));
    }
  }, [flat.length, selectedIndex]);

  function onPanelKeyDown(e: React.KeyboardEvent<HTMLDivElement>): void {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (flat.length === 0) return;
      setSelectedIndex((i) => (i + 1) % flat.length);
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (flat.length === 0) return;
      setSelectedIndex((i) => (i - 1 + flat.length) % flat.length);
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const item = flat[selectedIndex];
      if (item) item.execute();
    }
  }

  if (!mounted) return null;

  // Flat-index cursor used to map each rendered row to its
  // position in `flat[]` so hover / click reference the right
  // index across sections.
  let flatCursor = 0;

  return (
    <div
      role="presentation"
      onClick={onClose}
      onKeyDown={onPanelKeyDown}
      className={cn(
        "fixed inset-0 z-50 flex items-start justify-center pt-[14vh]",
        // Slightly stronger blur on the backdrop than the panel
        // would give us alone — pushes everything behind the
        // palette into pleasant unfocused-glass territory.
        "backdrop-blur-2xl backdrop-saturate-150",
        // Backdrop fades over 250ms.
        "transition-opacity duration-[250ms]",
        visible ? "opacity-100" : "opacity-0",
      )}
      style={{
        background:
          "color-mix(in srgb, var(--color-bg) 40%, transparent)",
        transitionTimingFunction: "var(--ease-out-quart)",
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "liquid-glass liquid-glass--panel",
          "relative isolate w-full max-w-[600px] overflow-hidden rounded-2xl",
          // Subtle accent ring at low opacity — the "this is the
          // active surface" cue without crossing the no-shadow rule.
          "ring-1 ring-[color:color-mix(in_srgb,var(--color-accent)_10%,transparent)]",
          // Two transitions: opacity over 200ms, transform with
          // spring over 300ms. Reduced motion users skip the
          // transform path entirely via the @media guard below.
          "transition-[opacity] duration-200",
          visible ? "opacity-100" : "opacity-0",
        )}
        style={{
          maxHeight: "min(60vh, 560px)",
          // Spring transform handled separately so we can keep the
          // opacity-only transition on the same element with
          // different timing function + duration.
          transform: visible ? "scale(1)" : "scale(0.94)",
          transition:
            "opacity 200ms var(--ease-out-quart), transform 300ms var(--ease-spring)",
        }}
      >
        {/* Search row: recessed glass with the liquid-glass--inner
            search-icon plate to its left and the esc-hint kbd to
            the right. Hairline rule below. */}
        <div
          className="relative z-10 flex items-center gap-3 px-4 py-3"
          style={{
            // Inset shadow at the bottom gives a subtle "the input
            // is recessed into the panel" feel — Apple's standard
            // pane-divider depth cue.
            boxShadow: "inset 0 -1px 0 0 var(--color-hairline)",
          }}
        >
          <span
            aria-hidden
            className={cn(
              "liquid-glass--inner",
              "inline-flex h-7 w-7 items-center justify-center rounded-lg",
            )}
          >
            <Search className="h-3.5 w-3.5 text-muted" strokeWidth={2} />
          </span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setSelectedIndex(0);
            }}
            placeholder="Search pages and actions…"
            aria-label="Command palette search"
            // The accent caret is a tiny detail that makes the
            // input feel "alive" against the glass.
            style={{ caretColor: "var(--color-accent)" }}
            className={cn(
              "flex-1 bg-transparent text-[15px] text-ink placeholder:text-muted",
              "selection:bg-[color:color-mix(in_srgb,var(--color-accent)_30%,transparent)]",
              "focus:outline-none",
            )}
          />
          <kbd
            className={cn(
              "liquid-glass--inner",
              "inline-flex h-5 items-center justify-center rounded-[6px] px-1.5",
              "font-mono text-[10px] font-medium tabular-nums text-muted",
            )}
          >
            esc
          </kbd>
        </div>

        <div
          className="relative z-10 overflow-y-auto px-2 py-2"
          style={{ maxHeight: "calc(min(60vh, 560px) - 56px - 36px)" }}
        >
          {flat.length === 0 ? (
            <p className="px-3 py-6 text-center text-sm text-muted">
              No matches.
            </p>
          ) : (
            <>
              {sections.recent.length > 0 ? (
                <SectionHeading first>Recent</SectionHeading>
              ) : null}
              {sections.recent.map((row) => {
                const i = flatCursor++;
                return (
                  <ResultRow
                    key={row.key}
                    row={row}
                    index={i}
                    selected={i === selectedIndex}
                    onHover={() => setSelectedIndex(i)}
                  />
                );
              })}
              {sections.pages.length > 0 ? (
                <SectionHeading first={sections.recent.length === 0}>
                  Pages
                </SectionHeading>
              ) : null}
              {sections.pages.map((row) => {
                const i = flatCursor++;
                return (
                  <ResultRow
                    key={row.key}
                    row={row}
                    index={i}
                    selected={i === selectedIndex}
                    onHover={() => setSelectedIndex(i)}
                  />
                );
              })}
              {sections.actions.length > 0 ? (
                <SectionHeading
                  first={
                    sections.recent.length === 0 && sections.pages.length === 0
                  }
                >
                  Actions
                </SectionHeading>
              ) : null}
              {sections.actions.map((row) => {
                const i = flatCursor++;
                return (
                  <ResultRow
                    key={row.key}
                    row={row}
                    index={i}
                    selected={i === selectedIndex}
                    onHover={() => setSelectedIndex(i)}
                  />
                );
              })}
            </>
          )}
        </div>

        <div
          className="relative z-10 flex items-center justify-between px-4 py-2 text-[10px] text-muted"
          style={{
            boxShadow: "inset 0 1px 0 0 var(--color-hairline)",
          }}
        >
          <span className="font-mono tabular-nums">
            {modifierKey(platform)}K to open · ↑↓ navigate · enter to select
          </span>
        </div>
      </div>
    </div>
  );
}

function SectionHeading({
  children,
  first,
}: {
  children: React.ReactNode;
  first?: boolean;
}): React.ReactElement {
  return (
    <div className={cn(first ? "pt-1" : "pt-3")}>
      {/* Fading horizontal rule above the heading (skipped on the
          first section so the search input's bottom edge serves
          as the divider). */}
      {first ? null : <div className="pane-rule mx-3 mb-2" aria-hidden />}
      <p className="mb-1 px-3 text-[10px] uppercase tracking-eyebrow text-muted">
        {children}
      </p>
    </div>
  );
}

function ResultRow({
  row,
  index,
  selected,
  onHover,
}: {
  row: ResultBase;
  index: number;
  selected: boolean;
  onHover: () => void;
}): React.ReactElement {
  // Stagger fade-in cap at 240 ms total (8 rows × 30 ms). Pure CSS
  // — the row is rendered into the DOM all at once; only the
  // animation start time differs.
  const delay = Math.min(index * 30, 240);
  return (
    <button
      type="button"
      onMouseEnter={onHover}
      onClick={row.execute}
      className={cn(
        "group relative isolate flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-left",
        "animate-[paletteRowIn_220ms_ease-out_both]",
        // Smooth highlight cross-fade between rows (150 ms) so
        // arrow-key navigation doesn't snap — feels like a moving
        // selection rather than a flicker.
        "transition-[background-color,box-shadow] duration-150",
      )}
      style={{
        animationDelay: `${delay}ms`,
        backgroundColor: selected
          ? "var(--glass-accent-tint)"
          : "transparent",
        boxShadow: selected
          ? "inset 0 0 0 1px color-mix(in srgb, var(--color-accent) 28%, transparent), inset 0 1px 0 0 color-mix(in srgb, var(--color-accent) 18%, transparent)"
          : "none",
      }}
      data-row-index={index}
    >
      {/* Hover-only soft glass highlight. Layered under the
          selected-state accent tint so a hovered+selected row
          shows the accent. Pointer-events-none so it doesn't
          steal mouse events from the row itself. */}
      <span
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-0 rounded-xl opacity-0 transition-opacity duration-150",
          "group-hover:opacity-100",
        )}
        style={{
          background:
            "color-mix(in srgb, var(--color-ink) 4%, transparent)",
        }}
      />
      <span
        className={cn(
          "liquid-glass--inner",
          "relative z-10 inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
        )}
      >
        {row.icon}
      </span>
      <span className="relative z-10 flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[14px] text-ink">{row.title}</span>
        {row.subtitle ? (
          <span className="truncate font-mono text-[11px] tabular-nums text-muted">
            {row.subtitle}
          </span>
        ) : null}
      </span>
      {row.kbdHint ? (
        <kbd
          className={cn(
            "liquid-glass--inner",
            "relative z-10 inline-flex h-5 items-center justify-center rounded-[6px] px-1.5",
            "font-mono text-[10px] font-medium tabular-nums text-muted",
          )}
        >
          {row.kbdHint}
        </kbd>
      ) : null}
    </button>
  );
}
