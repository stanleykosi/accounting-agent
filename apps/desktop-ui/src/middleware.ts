/*
Purpose: Enforce protected desktop route access and validate browser sessions at navigation time.
Scope: Redirect anonymous users to login, keep stale cookies from causing redirect loops, and forward validated session data to protected layouts.
Dependencies: Next.js middleware APIs and the canonical desktop auth session helpers.
*/

import { NextResponse, type NextRequest } from "next/server";
import {
  AUTH_COOKIE_NAME,
  AUTH_SESSION_HEADER_NAME,
  buildLoginRedirectPath,
  resolvePostLoginPath,
  serializeSessionHeader,
  toSessionRedirectReason,
  validateSessionCookie,
} from "./lib/auth/session";
import { isHostedFrontendRuntime } from "./lib/runtime";

const PUBLIC_FILE_PATTERN = /\.[^/]+$/u;
const SETUP_PATH = "/setup";

/**
 * Purpose: Guard protected workspace routes and bounce authenticated operators away from the login screen.
 * Inputs: The current incoming Next.js request.
 * Outputs: A redirect, a cookie-clearing recovery response, or a forwarded protected request.
 * Behavior: Uses the canonical FastAPI session endpoint as the single source of truth for browser session validity.
 */
export async function middleware(request: NextRequest): Promise<NextResponse> {
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

  const validationResult = await validateSessionCookie(request.headers.get("cookie"));
  if (!validationResult.ok) {
    if (isLoginPath) {
      const response = NextResponse.next();
      response.cookies.delete(AUTH_COOKIE_NAME);
      return response;
    }

    return redirectToLogin(request, toSessionRedirectReason(validationResult.error));
  }

  if (isLoginPath) {
    const nextPath = resolvePostLoginPath(request.nextUrl.searchParams.get("next"));
    const response = NextResponse.redirect(new URL(nextPath, request.url));
    applyRotatedCookie(response, validationResult.setCookieHeader);
    return response;
  }

  const forwardedHeaders = new Headers(request.headers);
  forwardedHeaders.set(AUTH_SESSION_HEADER_NAME, serializeSessionHeader(validationResult.session));
  const response = NextResponse.next({
    request: {
      headers: forwardedHeaders,
    },
  });
  applyRotatedCookie(response, validationResult.setCookieHeader);
  return response;
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
