"use client";

/**
 * Generic 30-second polling hook for the trader dashboard.
 *
 * Returns:
 *   - `data`           : latest successful response, or null on first load / hard reset.
 *   - `error`          : last fetch error, cleared on next success.
 *   - `isLoading`      : true on the very first load (data === null && error === null).
 *   - `lastUpdatedAt`  : Date of last successful fetch, or null.
 *   - `refresh()`      : manual trigger (resets the timer + immediately re-fetches).
 *
 * No SWR / React Query — matches the existing codebase pattern of
 * "raw fetch + tiny helper". 30s default cadence per the dashboard
 * spec. Cancels in-flight requests on unmount via AbortController.
 *
 * The hook is intentionally permissive about T's shape — it doesn't
 * know the response schema; callers narrow via traderFetch<T>.
 */

import { useCallback, useEffect, useRef, useState } from "react";

interface PollingState<T> {
  data: T | null;
  error: Error | null;
  isLoading: boolean;
  lastUpdatedAt: Date | null;
  refresh: () => void;
}

export interface UsePollingOptions<T> {
  intervalMs?: number;
  fetcher: (_signal: AbortSignal) => Promise<T>;
  // When false, the hook is paused (e.g. page hidden). Default true.
  enabled?: boolean;
}

export function usePolling<T>(options: UsePollingOptions<T>): PollingState<T> {
  const { intervalMs = 30_000, fetcher, enabled = true } = options;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null);

  // refresh() is generally a manual trigger; ref so the effect doesn't
  // restart on every re-render that captures it. fetcher is similar —
  // ref it so a new fetcher closure doesn't re-fire the effect.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const tickRef = useRef<NodeJS.Timeout | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const doFetch = useCallback(async (): Promise<void> => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const next = await fetcherRef.current(controller.signal);
      if (controller.signal.aborted) return;
      setData(next);
      setError(null);
      setLastUpdatedAt(new Date());
    } catch (e) {
      if (controller.signal.aborted) return;
      // DOMException with name "AbortError" is the normal cancellation
      // path. Anything else is a real error worth surfacing.
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e instanceof Error ? e : new Error(String(e)));
    }
  }, []);

  const refresh = useCallback((): void => {
    if (tickRef.current) {
      clearTimeout(tickRef.current);
      tickRef.current = null;
    }
    void doFetch();
  }, [doFetch]);

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;

    const loop = async (): Promise<void> => {
      if (cancelled) return;
      await doFetch();
      if (cancelled) return;
      tickRef.current = setTimeout(loop, intervalMs);
    };

    void loop();

    return () => {
      cancelled = true;
      if (tickRef.current) {
        clearTimeout(tickRef.current);
        tickRef.current = null;
      }
      controllerRef.current?.abort();
    };
  }, [doFetch, intervalMs, enabled]);

  return {
    data,
    error,
    isLoading: data === null && error === null,
    lastUpdatedAt,
    refresh,
  };
}
