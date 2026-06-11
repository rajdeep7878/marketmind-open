import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * /admin/* HTTP basic auth gate.
 *
 * Runs ahead of every /admin route. Validates the Authorization header
 * against ADMIN_USERNAME / ADMIN_PASSWORD from the env. When either is
 * missing the page returns 503 (admin disabled); when credentials are
 * absent or wrong we return 401 with WWW-Authenticate so the browser
 * prompts.
 *
 * Note: this is also enforced API-side. Two-layer protection is
 * intentional — direct API access doesn't go through the web app.
 *
 * Env vars used here are NOT exposed to the client (no NEXT_PUBLIC_
 * prefix), so the secrets stay server-side.
 */

const REALM = "MarketMind admin";

function unauthorized(): NextResponse {
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": `Basic realm="${REALM}"` },
  });
}

function adminDisabled(): NextResponse {
  return new NextResponse("Admin disabled", { status: 503 });
}

export function middleware(req: NextRequest): NextResponse {
  const username = process.env.ADMIN_USERNAME;
  const password = process.env.ADMIN_PASSWORD;
  if (!username || !password) {
    return adminDisabled();
  }

  const header = req.headers.get("authorization");
  if (!header || !header.toLowerCase().startsWith("basic ")) {
    return unauthorized();
  }

  let provided: string;
  try {
    provided = atob(header.slice(6).trim());
  } catch {
    return unauthorized();
  }
  const [user, ...pwParts] = provided.split(":");
  const pw = pwParts.join(":");
  if (user !== username || pw !== password) {
    return unauthorized();
  }

  return NextResponse.next();
}

export const config = {
  // Only run the middleware on /admin/*.
  matcher: ["/admin/:path*"],
};
