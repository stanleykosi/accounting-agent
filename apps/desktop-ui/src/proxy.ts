/*
Purpose: Enforce protected desktop route access with one canonical cookie gate.
Scope: Redirect anonymous users to login, preserve local runtime setup routing, and only validate sessions on the login screen handoff.
Dependencies: Next.js proxy APIs and the canonical desktop auth session helpers.
*/

import { NextResponse, type NextRequest } from "next/server";
import {
  AUTH_COOKIE_NAME,
  buildLoginRedirectPath,
  resolvePostLoginPath,
  validateSessionCookie,
} from "./lib/auth/session";
import { isHostedFrontendRuntime } from "./lib/runtime";

const PUBLIC_FILE_PATTERN = /\.[^/]+$/u;
const SETUP_PATH = "/setup";

/**
 * Purpose: Guard protected workspace routes and bounce authenticated operators away from the login screen.
 * Inputs: The current incoming Next.js request.
 * Outputs: A redirect, a cookie-clearing recovery response, or the admitted protected request.
 * Behavior: Uses cookie presence as the protected-route gate so route loads do not depend on a server-side auth roundtrip.
 */
export async function proxy(request: NextRequest): Promise<NextResponse> {
  const { pathname } = request.nextUrl;
  if (shouldBypassPath(pathname)) {
    return NextResponse.next();
  }

  const isSetupPath = pathname === SETUP_PATH;
  const hasSessionCookie = request.cookies.has(AUTH_COOKIE_NAME);
  const isLoginPath = pathname === "/login";
  if (!isHostedFrontendRuntime() && !isLoginPath && !isSetupPath) {
    const runtimeReady = await isLocalRuntimeReady(request);
    if (!runtimeReady) {
      return redirectToSetup(request);
    }
  }

  if (isSetupPath) {
    return NextResponse.next();
  }

  if (!hasSessionCookie) {
    if (isLoginPath) {
      return NextResponse.next();
    }

    return redirectToLogin(request, "auth-required");
  }

  if (isLoginPath) {
    try {
      const validationResult = await validateSessionCookie(request.headers.get("cookie"));
      if (!validationResult.ok) {
        const response = NextResponse.next();
        response.cookies.delete(AUTH_COOKIE_NAME);
        return response;
      }

      const nextPath = resolvePostLoginPath(request.nextUrl.searchParams.get("next"));
      const response = NextResponse.redirect(new URL(nextPath, request.url));
      applyRotatedCookie(response, validationResult.setCookieHeader);
      return response;
    } catch {
      return NextResponse.next();
    }
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico).*)"],
};

function redirectToLogin(
  request: NextRequest,
  reason: "auth-required" | "session-expired" | "user-disabled",
): NextResponse {
  const nextPath = `${request.nextUrl.pathname}${request.nextUrl.search}`;
  const response = NextResponse.redirect(
    new URL(
      buildLoginRedirectPath({
        nextPath,
        reason,
      }),
      request.url,
    ),
  );
  response.cookies.delete(AUTH_COOKIE_NAME);
  return response;
}

function shouldBypassPath(pathname: string): boolean {
  return (
    pathname.startsWith("/_next") ||
    pathname === "/favicon.ico" ||
    PUBLIC_FILE_PATTERN.test(pathname)
  );
}

function applyRotatedCookie(response: NextResponse, setCookieHeader: string | null): void {
  if (setCookieHeader === null || setCookieHeader.length === 0) {
    return;
  }

  response.headers.append("set-cookie", setCookieHeader);
}

async function isLocalRuntimeReady(request: NextRequest): Promise<boolean> {
  try {
    const response = await fetch(new URL("/api/setup/health", request.url), {
      cache: "no-store",
      headers: {
        Accept: "application/json",
      },
      signal: AbortSignal.timeout(2_500),
    });
    if (!response.ok) {
      return false;
    }

    const payload = (await response.json()) as { ready?: boolean };
    return payload.ready === true;
  } catch {
    return false;
  }
}

function redirectToSetup(request: NextRequest): NextResponse {
  const nextPath = `${request.nextUrl.pathname}${request.nextUrl.search}`;
  const setupUrl = new URL(SETUP_PATH, request.url);
  if (nextPath !== SETUP_PATH) {
    setupUrl.searchParams.set("next", nextPath);
  }

  return NextResponse.redirect(setupUrl);
}
