"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { CommandPalette } from "@/components/nav/command-palette";
import { NavRail } from "@/components/nav/nav-rail";

/**
 * Mounts the NavRail (fixed top-right) and the CommandPalette
 * modal. Owns:
 *
 *   - the open/closed state
 *   - the global ⌘K / Ctrl+K keydown listener
 *   - the body-scroll lock during the open state
 *   - the trigger ref the palette uses to restore focus on close
 *
 * Exposes `openPalette()` via React context for any descendant
 * (currently only the NavRail's pill consumes it). Putting the
 * context here keeps the provider self-contained.
 *
 * The provider renders <NavRail /> + <CommandPalette /> as
 * siblings to {children}; the rail is fixed-positioned so it
 * never affects page layout.
 *
 * Scroll lock (per build adjustment 2):
 *   On open, we capture the body's current `overflow` and set it
 *   to "hidden"; on close, restore the captured value. Using the
 *   captured value (not a hardcoded "auto"/"visible") avoids
 *   clobbering whatever Tailwind / page-level CSS had set.
 */

interface PaletteContextValue {
  openPalette: () => void;
  closePalette: () => void;
  isOpen: boolean;
}

const PaletteContext = createContext<PaletteContextValue | null>(null);

export function useCommandPalette(): PaletteContextValue {
  const ctx = useContext(PaletteContext);
  if (ctx === null) {
    throw new Error(
      "useCommandPalette must be used inside <CommandPaletteProvider>",
    );
  }
  return ctx;
}

/**
 * Top-level wrapper for the entire app. Render once in the root
 * layout, then page content as children.
 */
export function CommandPaletteProvider({
  children,
}: {
  children: React.ReactNode;
}): React.ReactElement {
  const [isOpen, setIsOpen] = useState(false);
  const triggerRef = useRef<HTMLElement | null>(null);

  const openPalette = useCallback(() => {
    // Capture the element that requested the open so we can
    // restore focus when the palette closes. The pill (when
    // visible) is the click trigger; for keyboard-only users
    // (mobile, no pill, ⌘K still works) we capture whatever was
    // focused at the moment.
    if (typeof document !== "undefined") {
      const active = document.activeElement;
      if (active instanceof HTMLElement) {
        triggerRef.current = active;
      }
    }
    setIsOpen(true);
  }, []);

  const closePalette = useCallback(() => {
    setIsOpen(false);
  }, []);

  // Global ⌘K / Ctrl+K. Mounted once at provider lifetime.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key !== "k" && e.key !== "K") return;
      // metaKey on macOS, ctrlKey elsewhere — we accept either so
      // the shortcut works regardless of detection accuracy.
      if (!e.metaKey && !e.ctrlKey) return;
      // Avoid trapping the browser's "select address bar" combo
      // (which is ⌘L on macOS, not ⌘K — but on Firefox ⌘K is
      // the search-bar focus and we DO want to override that
      // inside the app).
      e.preventDefault();
      setIsOpen((open) => !open);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Body scroll lock while the palette is open. Captures the
  // previous value so close restores rather than guessing.
  useEffect(() => {
    if (!isOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [isOpen]);

  const ctxValue = useMemo<PaletteContextValue>(
    () => ({ openPalette, closePalette, isOpen }),
    [openPalette, closePalette, isOpen],
  );

  return (
    <PaletteContext.Provider value={ctxValue}>
      {children}
      <NavRail onOpenPalette={openPalette} />
      <CommandPalette
        open={isOpen}
        onClose={closePalette}
        triggerRef={triggerRef as React.RefObject<HTMLElement>}
      />
    </PaletteContext.Provider>
  );
}
