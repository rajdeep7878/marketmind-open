"use client";

/**
 * Admin auth + admin-fetch helpers for the trader dashboard.
 *
 * Pattern (per Step-13's design):
 *   1. User clicks an admin action (Pause, Resume, Approve).
 *   2. If no creds in sessionStorage → show the modal.
 *   3. Modal validates creds against /trader/admin/auth_check BEFORE
 *      storing them. Only 200 → sessionStorage write → caller retries.
 *   4. Subsequent admin actions reuse the stored creds.
 *   5. Any admin call returning 401 clears sessionStorage and re-
 *      throws a sentinel error so the caller can re-prompt.
 *
 * Storage:
 *   - sessionStorage (NOT localStorage) so creds are wiped when the
 *     browser closes. Acceptable for localhost-only v1; a cloud
 *     deployment should upgrade to a real session token.
 *
 * Limitation acknowledged:
 *   sessionStorage is XSS-readable. The trader dashboard is the only
 *   surface that ever holds these creds in JS memory, and it's
 *   intended for local dev only. For cloud, replace with HTTP-only
 *   session cookies issued by the API after a server-side login.
 */

import { apiBaseUrl } from "@/lib/env";

const SESSION_KEY = "marketmind-trader-admin";

export interface AdminCredentials {
  username: string;
  password: string;
}

/** Sentinel thrown when an admin action needs the modal. */
export const ADMIN_AUTH_REQUIRED = "admin_auth_required";

/** Sentinel thrown when creds were rejected and need re-entry. */
export const ADMIN_AUTH_INVALID = "admin_auth_invalid";

/** Sentinel thrown when the server has admin auth disabled (503). */
export const ADMIN_AUTH_DISABLED = "admin_auth_disabled";

function readCredsFromSession(): AdminCredentials | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AdminCredentials;
    if (typeof parsed.username !== "string" || typeof parsed.password !== "string") {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

function writeCredsToSession(creds: AdminCredentials): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(creds));
  } catch {
    // Storage disabled / private mode — the modal will keep
    // re-prompting per admin action. Acceptable degradation.
  }
}

export function clearAdminCreds(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // ignore
  }
}

export function hasAdminCreds(): boolean {
  return readCredsFromSession() !== null;
}

function basicAuthHeader(creds: AdminCredentials): string {
  // btoa is fine for username + password ASCII strings; admin creds
  // are operator-controlled and won't contain UTF-8.
  const token = `${creds.username}:${creds.password}`;
  return `Basic ${btoa(token)}`;
}

/**
 * Probe the auth_check endpoint with the supplied creds.
 * Returns true on 200, false on 401, throws on 503 (admin disabled
 * server-side — the user should be told, not re-prompted).
 */
export async function validateAdminCreds(
  creds: AdminCredentials,
  signal?: AbortSignal,
): Promise<boolean> {
  const res = await fetch(`${apiBaseUrl()}/trader/admin/auth_check`, {
    headers: { Authorization: basicAuthHeader(creds) },
    cache: "no-store",
    ...(signal ? { signal } : {}),
  });
  if (res.status === 200) return true;
  if (res.status === 401) return false;
  if (res.status === 503) {
    throw new Error(ADMIN_AUTH_DISABLED);
  }
  throw new Error(`auth_check -> unexpected ${res.status}`);
}

/**
 * Validate then persist. Returns true on success; false on 401.
 * Caller surfaces the right message and either re-prompts or
 * proceeds with the originally-requested action.
 */
export async function setAdminCreds(
  creds: AdminCredentials,
  signal?: AbortSignal,
): Promise<boolean> {
  const ok = await validateAdminCreds(creds, signal);
  if (ok) writeCredsToSession(creds);
  return ok;
}

/**
 * Wrapper around fetch() that injects the stored Basic header.
 *
 * Throws ADMIN_AUTH_REQUIRED if no creds stored — caller should
 * open the modal. Throws ADMIN_AUTH_INVALID if the request comes
 * back 401 — clears storage and signals re-prompt.
 */
export async function traderAdminFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const creds = readCredsFromSession();
  if (creds === null) {
    throw new Error(ADMIN_AUTH_REQUIRED);
  }
  const res = await fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      ...(init.headers ?? {}),
      Authorization: basicAuthHeader(creds),
    },
  });
  if (res.status === 401) {
    clearAdminCreds();
    throw new Error(ADMIN_AUTH_INVALID);
  }
  if (res.status === 503) {
    throw new Error(ADMIN_AUTH_DISABLED);
  }
  return res;
}

/** Convenience wrappers for the three admin actions. */
export async function disableStrategy(versionId: string): Promise<void> {
  const res = await traderAdminFetch(`/trader/strategies/${versionId}/disable`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`disable -> ${res.status}`);
}

export async function enableStrategy(versionId: string): Promise<void> {
  const res = await traderAdminFetch(`/trader/strategies/${versionId}/enable`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`enable -> ${res.status}`);
}

export async function approvePaper(versionId: string): Promise<void> {
  const res = await traderAdminFetch(`/trader/strategies/${versionId}/approve_paper`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`approve_paper -> ${res.status}`);
}
