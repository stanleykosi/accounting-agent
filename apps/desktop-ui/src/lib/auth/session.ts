/*
Purpose: Centralize server and middleware auth-session helpers for the desktop UI.
Scope: Backend auth endpoint resolution, login-route validation, redirect targets, and cookie introspection.
Dependencies: The shared auth client contracts and the FastAPI auth session endpoint.
*/

import {
  AuthApiError,
  buildAuthApiError,
  parseAuthSessionResponse,
  type AuthSessionResponse,
} from "./client";
import { fetchBackendWithAvailabilityRetry } from "../backend-proxy";
import { resolveBackendApiBaseUrl } from "../runtime";

export const AUTH_COOKIE_NAME =
  process.env.ACCOUNTING_AGENT_SESSION_COOKIE_NAME ?? "accounting_agent_session";
export const DEFAULT_WORKSPACE_PATH = "/";
const SESSION_VALIDATION_CACHE_TTL_MS = 10_000;

type CachedSessionValidationEntry = Readonly<{
  expiresAt: number;
  result: SessionValidationResult;
}>;

const sessionValidationCache = new Map<string, CachedSessionValidationEntry>();
const inFlightSessionValidationCache = new Map<string, Promise<SessionValidationResult>>();

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

  const sessionToken = extractSessionToken(cookieHeader);
  if (sessionToken === null) {
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

  const cachedResult = readCachedSessionValidation(sessionToken);
  if (cachedResult !== null) {
    return cachedResult;
  }

  const inFlightValidation = inFlightSessionValidationCache.get(sessionToken);
  if (inFlightValidation !== undefined) {
    return inFlightValidation;
  }

  const nextValidation = fetchBackendWithAvailabilityRetry(buildBackendAuthUrl("/session"), {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      Cookie: cookieHeader,
    },
    method: "GET",
    redirect: "manual",
  })
    .then(async (response) => {
      const payload = await parseJsonPayload(response);
      const setCookieHeader = response.headers.get("set-cookie");
      if (!response.ok) {
        if (response.status >= 500) {
          throw new Error("Auth backend is temporarily unavailable.");
        }
        return {
          error: buildAuthApiError(response.status, payload),
          ok: false,
          setCookieHeader,
        } satisfies SessionValidationResult;
      }

      const result = {
        ok: true,
        session: parseAuthSessionResponse(payload),
        setCookieHeader,
      } satisfies SessionValidationResult;
      writeCachedSessionValidation(sessionToken, result);
      return result;
    })
    .finally(() => {
      inFlightSessionValidationCache.delete(sessionToken);
    });

  inFlightSessionValidationCache.set(sessionToken, nextValidation);
  return nextValidation;
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

function readCachedSessionValidation(sessionToken: string): SessionValidationResult | null {
  clearExpiredSessionValidationCache();
  const cachedEntry = sessionValidationCache.get(sessionToken);
  if (cachedEntry === undefined || cachedEntry.expiresAt <= Date.now()) {
    sessionValidationCache.delete(sessionToken);
    return null;
  }

  return cachedEntry.result;
}

function writeCachedSessionValidation(sessionToken: string, result: SessionValidationResult): void {
  clearExpiredSessionValidationCache();
  sessionValidationCache.set(sessionToken, {
    expiresAt: Date.now() + SESSION_VALIDATION_CACHE_TTL_MS,
    result,
  });
}

function clearExpiredSessionValidationCache(): void {
  const now = Date.now();
  for (const [sessionToken, cachedEntry] of sessionValidationCache.entries()) {
    if (cachedEntry.expiresAt <= now) {
      sessionValidationCache.delete(sessionToken);
    }
  }
}

function extractSessionToken(cookieHeader: string): string | null {
  const cookieSegments = cookieHeader.split(";");
  for (const segment of cookieSegments) {
    const [rawName, ...rawValueParts] = segment.split("=");
    if (typeof rawName !== "string") {
      continue;
    }

    const cookieName = rawName.trim();
    if (cookieName !== AUTH_COOKIE_NAME) {
      continue;
    }

    const cookieValue = rawValueParts.join("=").trim();
    return cookieValue.length > 0 ? cookieValue : null;
  }

  return null;
}
