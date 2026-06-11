"use client";

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  ADMIN_AUTH_DISABLED,
  setAdminCreds,
  type AdminCredentials,
} from "@/lib/trader-admin";
import { cn } from "@/lib/utils";

/**
 * Modal credential prompt for admin actions.
 *
 * Behaviour required by the spec:
 *   - Validates creds against /trader/admin/auth_check BEFORE
 *     storing in sessionStorage (no "store-then-fail-then-clear"
 *     UX dance).
 *   - Focus trap inside the modal while open: Tab cycles between
 *     focusable elements (two inputs + cancel button + submit
 *     button).
 *   - Esc cancels.
 *   - Click on the backdrop cancels.
 *   - Restores focus to the element that opened the modal when
 *     it closes.
 *
 * Radix Dialog deliberately NOT pulled in — the codebase only uses
 * `@radix-ui/react-separator` and `@radix-ui/react-slot`. One modal
 * doesn't justify the bundle cost.
 */

interface Props {
  open: boolean;
  onSuccess: () => void;
  onCancel: () => void;
}

export function AdminCredentialsModal({
  open,
  onSuccess,
  onCancel,
}: Props): React.ReactElement | null {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const userInputRef = useRef<HTMLInputElement>(null);
  const passInputRef = useRef<HTMLInputElement>(null);
  const cancelBtnRef = useRef<HTMLButtonElement>(null);
  const submitBtnRef = useRef<HTMLButtonElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  // Capture the trigger element so we can restore focus on close.
  // Focus the first input when the modal opens.
  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = (document.activeElement as HTMLElement | null) ?? null;
    setUsername("");
    setPassword("");
    setError(null);
    setSubmitting(false);
    // requestAnimationFrame so the focus call lands after Tailwind
    // applies layout — focus on a hidden element is silently dropped
    // by the browser otherwise.
    const id = requestAnimationFrame(() => userInputRef.current?.focus());
    return () => cancelAnimationFrame(id);
  }, [open]);

  useEffect(() => {
    if (open) return;
    // Restore focus once the modal unmounts cleanly.
    const prev = previouslyFocused.current;
    if (prev && document.contains(prev)) prev.focus();
  }, [open]);

  if (!open) return null;

  function close(): void {
    onCancel();
  }

  async function handleSubmit(e: React.FormEvent): Promise<void> {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    const creds: AdminCredentials = { username, password };
    try {
      const ok = await setAdminCreds(creds);
      if (ok) {
        onSuccess();
      } else {
        setError("Username or password didn't match.");
      }
    } catch (e) {
      if (e instanceof Error && e.message === ADMIN_AUTH_DISABLED) {
        setError("Admin auth is disabled on the server (ADMIN_USERNAME unset).");
      } else {
        setError(`Couldn't reach the API: ${e instanceof Error ? e.message : String(e)}`);
      }
    } finally {
      setSubmitting(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLDivElement>): void {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
      return;
    }
    if (e.key !== "Tab") return;

    // Manual focus-trap: cycle between the four focusable elements.
    // Order matches DOM order so the visual + logical traversals match.
    const order: ReadonlyArray<HTMLElement | null> = [
      userInputRef.current,
      passInputRef.current,
      cancelBtnRef.current,
      submitBtnRef.current,
    ];
    const focusables = order.filter(
      (el): el is HTMLElement => el !== null && !el.hasAttribute("disabled"),
    );
    const first = focusables.at(0);
    const last = focusables.at(-1);
    if (!first || !last) return;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return (
    <div
      // Backdrop. Click cancels; the inner panel stops propagation.
      role="presentation"
      onClick={close}
      onKeyDown={handleKeyDown}
      className="fixed inset-0 z-50 flex items-center justify-center bg-[color:var(--color-bg)]/70 backdrop-blur-[1px]"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-modal-title"
        onClick={(e) => e.stopPropagation()}
        className={cn(
          "w-full max-w-sm rounded-sm border border-hairline bg-surface p-6",
          "shadow-none",
        )}
      >
        <p className="eyebrow">Admin auth</p>
        <h2 id="admin-modal-title" className="mt-2 font-serif text-xl text-ink">
          Enter admin credentials
        </h2>
        <p className="mt-3 text-sm leading-relaxed text-muted">
          Pause / resume / approve actions require the same{" "}
          <span className="font-mono text-xs tabular-nums">ADMIN_USERNAME</span> /{" "}
          <span className="font-mono text-xs tabular-nums">ADMIN_PASSWORD</span> used by the API.
          Credentials are kept in this tab only and cleared on close.
        </p>
        <form onSubmit={handleSubmit} className="mt-6 flex flex-col gap-4">
          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Username</span>
            <input
              ref={userInputRef}
              type="text"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              className="rounded-sm border border-hairline bg-bg px-3 py-2 font-mono text-sm tabular-nums text-ink focus:border-accent focus:outline-none"
              required
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Password</span>
            <input
              ref={passInputRef}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="rounded-sm border border-hairline bg-bg px-3 py-2 font-mono text-sm tabular-nums text-ink focus:border-accent focus:outline-none"
              required
            />
          </label>
          {error ? (
            <p className="callout text-sm text-negative" role="alert">
              {error}
            </p>
          ) : null}
          <div className="mt-2 flex items-center justify-end gap-3">
            <Button
              ref={cancelBtnRef}
              type="button"
              intent="secondary"
              size="sm"
              onClick={close}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button
              ref={submitBtnRef}
              type="submit"
              intent="primary"
              size="sm"
              disabled={submitting || username.length === 0 || password.length === 0}
            >
              {submitting ? "Checking…" : "Sign in"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
