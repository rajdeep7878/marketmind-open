/**
 * Thin typed fetch wrapper to the backend.
 * Phase 0 only needs /health; Phase 5 will expand this and pull
 * request/response types from the generated schemas.
 */
import { apiBaseUrl } from "@/lib/env";

export interface HealthResponse {
  status: "ok" | "degraded";
  environment: string;
  postgres: "ok" | "down";
  redis: "ok" | "down";
}

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  // Spread `signal` conditionally so `undefined` isn't passed under
  // exactOptionalPropertyTypes: true (the RequestInit type forbids it).
  const res = await fetch(`${apiBaseUrl()}/health`, {
    cache: "no-store",
    ...(signal ? { signal } : {}),
  });
  if (!res.ok) {
    throw new Error(`Health check failed: ${res.status}`);
  }
  return (await res.json()) as HealthResponse;
}
