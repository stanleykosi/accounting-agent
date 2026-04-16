/*
Purpose: Centralize server and middleware auth-session helpers for the desktop UI.
Scope: Backend auth endpoint resolution, session validation, redirect targets, and middleware header serialization.
Dependencies: The shared auth client contracts and the FastAPI auth session endpoint.
*/

import {
  AuthApiError,
  buildAuthApiError,
  parseAuthSessionResponse,
  type AuthSessionResponse,
} from "./client";
import { resolveBackendApiBaseUrl } from "../runtime";

export const AUTH_COOKIE_NAME =
  process.env.ACCOUNTING_AGENT_SESSION_COOKIE_NAME ?? "accounting_agent_session";
export const AUTH_SESSION_HEADER_NAME = "x-accounting-auth-session";
export const DEFAULT_WORKSPACE_PATH = "/";

export type SessionRedirectReason = "auth-required" | "session-expired" | "user-disabled";

export type SessionValidationResult =
  | {
      ok: true;
      session: AuthSessionResponse;
      setCookieHeader: string | null;
    }
  | {
      error: AuthApiError;
      ok: false;
      setCookieHeader: string | null;
    };

/**
 * Purpose: Validate the current session cookie by calling the canonical FastAPI auth session endpoint.
 * Inputs: Raw cookie header forwarded from the incoming browser request.
 * Outputs: Either the authenticated session payload or a structured auth failure.
 * Behavior: Preserves rotated cookies so middleware can pass them through to the browser response.
 */
export async function validateSessionCookie(
  cookieHeader: string | null,
): Promise<SessionValidationResult> {
  if (cookieHeader === null || cookieHeader.trim().length === 0) {
    return {
      error: new AuthApiError({
        code: "session_required",
        message: "Sign in to continue.",
        statusCode: 401,
      }),
      ok: false,
      setCookieHeader: null,
    };
  }

  const response = await fetch(buildBackendAuthUrl("/session"), {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Cookie: cookieHeader,
    },
    method: "GET",
    redirect: "manual",
  });

  const payload = await parseJsonPayload(response);
  const setCookieHeader = response.headers.get("set-cookie");
  if (!response.ok) {
    return {
      error: buildAuthApiError(response.status, payload),
      ok: false,
      setCookieHeader,
    };
  }

  return {
    ok: true,
    session: parseAuthSessionResponse(payload),
    setCookieHeader,
  };
}

/**
 * Purpose: Serialize the validated auth session into a request header safe for middleware forwarding.
 * Inputs: The authenticated session payload returned by the FastAPI auth API.
 * Outputs: A URI-encoded JSON string suitable for request headers.
 * Behavior: Uses reversible URI encoding so the value remains cross-runtime and ASCII-safe.
 */
export function serializeSessionHeader(session: Readonly<AuthSessionResponse>): string {
  return encodeURIComponent(JSON.stringify(session));
}

/**
 * Purpose: Read the middleware-forwarded auth session from a request header.
 * Inputs: The serialized session header value forwarded by middleware.
 * Outputs: The typed auth session payload, or null when the header is absent or invalid.
 * Behavior: Fails closed by returning null on malformed data so protected layouts can re-route safely.
 */
export function deserializeSessionHeader(value: string | null): AuthSessionResponse | null {
  if (value === null || value.length === 0) {
    return null;
  }

  try {
    return parseAuthSessionResponse(JSON.parse(decodeURIComponent(value)));
  } catch {
    return null;
  }
}

/**
 * Purpose: Build the login route with deterministic recovery context after auth failures.
 * Inputs: An optional internal return path and the session failure reason to surface to the operator.
 * Outputs: A relative login route with query parameters for the next screen.
 * Behavior: Drops unsafe external redirect targets and keeps only same-origin application paths.
 */
export function buildLoginRedirectPath(options?: {
  nextPath?: string;
  reason?: SessionRedirectReason;
}): string {
  const searchParams = new URLSearchParams();
  const nextPath = sanitizeInternalPath(options?.nextPath);
  if (nextPath !== null && nextPath !== "/login") {
    searchParams.set("next", nextPath);
  }

  if (options?.reason) {
    searchParams.set("reason", options.reason);
  }

  const query = searchParams.toString();
  return query.length > 0 ? `/login?${query}` : "/login";
}

/**
 * Purpose: Collapse backend auth error codes into the UI redirect reasons used by login recovery.
 * Inputs: A structured auth API error produced by session validation.
 * Outputs: A stable login-route reason query value.
 * Behavior: Keeps the login page messaging grounded in the canonical auth API error vocabulary.
 */
export function toSessionRedirectReason(error: Readonly<AuthApiError>): SessionRedirectReason {
  if (error.code === "session_expired") {
    return "session-expired";
  }

  if (error.code === "user_disabled") {
    return "user-disabled";
  }

  return "auth-required";
}

/**
 * Purpose: Resolve the post-login destination from an untrusted query parameter.
 * Inputs: Raw `next` query value from the login page.
 * Outputs: A safe internal path or the canonical workspace route.
 * Behavior: Rejects absolute URLs and auth routes so navigation cannot escape the app shell.
 */
export function resolvePostLoginPath(nextPath: string | null | undefined): string {
  return sanitizeInternalPath(nextPath) ?? DEFAULT_WORKSPACE_PATH;
}

/**
 * Purpose: Build the canonical backend auth URL targeted by server-side auth validation.
 * Inputs: A route suffix under the FastAPI `/auth` router.
 * Outputs: A fully qualified backend auth URL.
 * Behavior: Uses one canonical backend base URL and strips duplicate slashes.
 */
export function buildBackendAuthUrl(path: string): string {
  const normalizedBaseUrl = resolveBackendApiBaseUrl();
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBaseUrl}/auth${normalizedPath}`;
}

/**
 * Purpose: Create a user-friendly display label from the authenticated operator profile.
 * Inputs: The authenticated session currently bound to the layout.
 * Outputs: A short initialism that can be shown in the top bar.
 * Behavior: Falls back to the email prefix when the full name does not provide alphabetic initials.
 */
export function getOperatorInitials(session: Readonly<AuthSessionResponse>): string {
  const nameParts = session.user.full_name
    .split(/\s+/u)
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
  const initials = nameParts
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");

  if (initials.length > 0) {
    return initials;
  }

  return session.user.email.slice(0, 2).toUpperCase();
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

function sanitizeInternalPath(nextPath: string | null | undefined): string | null {
  if (typeof nextPath !== "string" || nextPath.length === 0) {
    return null;
  }

  if (!nextPath.startsWith("/") || nextPath.startsWith("//")) {
    return null;
  }

  return nextPath;
}
