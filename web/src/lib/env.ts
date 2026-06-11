/**
 * Validate NEXT_PUBLIC_* env vars at module load time.
 * A typo in the env var name fails build, not at runtime.
 *
 * `NEXT_PUBLIC_API_URL` is the BROWSER-facing API URL: it's bundled
 * into the client JS, so it must point at an address the browser
 * (running on the host) can reach. In compose that's the port-mapped
 * host loopback (`http://localhost:8000`).
 *
 * `API_URL_INTERNAL` is the SERVER-side API URL used by Next.js
 * server components and route handlers during render. It runs inside
 * the web container, so it must point at the api via the compose
 * network's internal hostname (`http://api:8000`). Optional: outside
 * compose (e.g. `next dev` on the host) we fall back to
 * `NEXT_PUBLIC_API_URL`, which is the same address from both sides.
 *
 * The two URLs are kept separate because they're the same in dev-on-
 * host and different in compose — handling both layouts is what
 * `apiBaseFor()` below does.
 */
import { z } from "zod";

const envSchema = z.object({
  NEXT_PUBLIC_API_URL: z.string().url(),
});

const parsed = envSchema.safeParse({
  NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
});

if (!parsed.success) {
  // Fail loudly. Throwing here surfaces the error at first render
  // rather than producing inscrutable runtime fetch failures later.
  // eslint-disable-next-line no-console
  console.error("Invalid environment variables:", parsed.error.flatten().fieldErrors);
  throw new Error("Invalid environment variables");
}

export const env = parsed.data;

/**
 * Return the API base URL appropriate to the current execution
 * environment. Server-side (Node, during SSR / route handlers /
 * server components) we prefer the internal compose hostname;
 * client-side (browser) we use the host-reachable URL bundled into
 * the client JS.
 *
 * Note: `API_URL_INTERNAL` is deliberately NOT validated by the
 * `z.object` above — it's a server-only optional. Reading it via
 * `process.env.API_URL_INTERNAL` inside the conditional means it
 * never gets inlined into the client bundle (Next.js only bundles
 * `NEXT_PUBLIC_*` env reads).
 */
export function apiBaseUrl(): string {
  if (typeof window === "undefined") {
    const internal = process.env.API_URL_INTERNAL;
    if (internal && internal.length > 0) {
      return internal;
    }
  }
  return env.NEXT_PUBLIC_API_URL;
}
