/*
Purpose: Define browser-safe auth contracts and helpers for the desktop UI auth proxy routes.
Scope: Login, registration, logout, session reads, and deterministic auth error handling.
Dependencies: Fetch, the same-origin Next.js auth proxy, and strict TypeScript runtime guards.
*/

export type AuthErrorCode =
  | "duplicate_email"
  | "invalid_credentials"
  | "session_expired"
  | "session_required"
  | "user_disabled"
  | "validation_error"
  | "unknown_error";

export type AuthenticatedUser = {
  email: string;
  full_name: string;
  id: string;
  last_login_at: string | null;
  status: string;
};

export type SessionDetails = {
  expires_at: string;
  id: string;
  last_seen_at: string;
  rotated: boolean;
};

export type AuthSessionResponse = {
  session: SessionDetails;
  user: AuthenticatedUser;
};

export type LogoutResponse = {
  status: string;
};

export type LoginInput = {
  email: string;
  password: string;
};

export type RegistrationInput = LoginInput & {
  full_name: string;
};

export class AuthApiError extends Error {
  readonly code: AuthErrorCode;
  readonly statusCode: number;

  /**
   * Purpose: Construct a typed auth error surfaced by the desktop UI auth helpers.
   * Inputs: Stable auth error code, HTTP status code, and operator-facing recovery message.
   * Outputs: An Error instance that callers can branch on without string parsing.
   * Behavior: Preserves the original operator message while attaching structured auth metadata.
   */
  constructor({
    code,
    message,
    statusCode,
  }: Readonly<{
    code: AuthErrorCode;
    message: string;
    statusCode: number;
  }>) {
    super(message);
    this.name = "AuthApiError";
    this.code = code;
    this.statusCode = statusCode;
  }
}

const AUTH_PROXY_BASE_PATH = "/api/auth";

/**
 * Purpose: Register a new local operator account through the Next.js auth proxy.
 * Inputs: Email, display name, and plaintext password supplied by the operator.
 * Outputs: The authenticated session payload returned by the FastAPI auth API.
 * Behavior: Creates the account and relies on the API to issue the session cookie immediately.
 */
export async function registerUser(
  payload: Readonly<RegistrationInput>,
): Promise<AuthSessionResponse> {
  return authRequest<AuthSessionResponse>("/register", {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Exchange local credentials for a cookie-backed authenticated session.
 * Inputs: Canonical operator email and plaintext password.
 * Outputs: The authenticated session payload returned by the FastAPI auth API.
 * Behavior: Uses the same proxy path as registration so browser requests remain same-origin.
 */
export async function loginUser(payload: Readonly<LoginInput>): Promise<AuthSessionResponse> {
  return authRequest<AuthSessionResponse>("/login", {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Read the caller's current authenticated session via the same-origin auth proxy.
 * Inputs: None.
 * Outputs: The active user and session metadata when the cookie remains valid.
 * Behavior: Surfaces structured auth failures so UI callers can recover deterministically.
 */
export async function readCurrentSession(): Promise<AuthSessionResponse> {
  return authRequest<AuthSessionResponse>("/session", {
    method: "GET",
  });
}

/**
 * Purpose: Revoke the caller's active session cookie through the same-origin auth proxy.
 * Inputs: None.
 * Outputs: The logout acknowledgement payload from the auth API.
 * Behavior: Treats logout as idempotent and still clears the server-side session when present.
 */
export async function logoutUser(): Promise<LogoutResponse> {
  return authRequest<LogoutResponse>("/logout", {
    method: "POST",
  });
}

/**
 * Purpose: Identify whether an unknown thrown value is a structured auth API failure.
 * Inputs: Any caught JavaScript value.
 * Outputs: A type predicate for `AuthApiError`.
 * Behavior: Lets callers branch on auth-specific failures without unsafe casts.
 */
export function isAuthApiError(error: unknown): error is AuthApiError {
  return error instanceof AuthApiError;
}

/**
 * Purpose: Detect auth failures that require the caller to sign in again.
 * Inputs: Any thrown JavaScript value.
 * Outputs: True when the error means the current auth session is no longer usable.
 * Behavior: Collapses the canonical session-required, session-expired, and disabled-user states.
 */
export function isSessionAuthError(error: unknown): boolean {
  return (
    isAuthApiError(error) &&
    (error.code === "session_expired" ||
      error.code === "session_required" ||
      error.code === "user_disabled")
  );
}

/**
 * Purpose: Convert a raw API payload into the strict auth session contract used by the UI.
 * Inputs: Unknown JSON parsed from an auth API response body.
 * Outputs: A typed session response object.
 * Behavior: Fails fast when the response shape drifts from the canonical backend contract.
 */
export function parseAuthSessionResponse(payload: unknown): AuthSessionResponse {
  if (!isRecord(payload)) {
    throw new Error("Auth session response was not an object.");
  }

  const user = payload.user;
  const session = payload.session;
  if (!isRecord(user) || !isRecord(session)) {
    throw new Error("Auth session response is missing required session or user data.");
  }

  return {
    session: {
      expires_at: requireString(session.expires_at, "session.expires_at"),
      id: requireString(session.id, "session.id"),
      last_seen_at: requireString(session.last_seen_at, "session.last_seen_at"),
      rotated: requireBoolean(session.rotated, "session.rotated"),
    },
    user: {
      email: requireString(user.email, "user.email"),
      full_name: requireString(user.full_name, "user.full_name"),
      id: requireString(user.id, "user.id"),
      last_login_at: requireNullableString(user.last_login_at, "user.last_login_at"),
      status: requireString(user.status, "user.status"),
    },
  };
}

/**
 * Purpose: Convert a raw API payload into the strict logout acknowledgement contract.
 * Inputs: Unknown JSON parsed from an auth logout response body.
 * Outputs: A typed logout acknowledgement.
 * Behavior: Fails fast when the backend contract drifts instead of silently ignoring the mismatch.
 */
export function parseLogoutResponse(payload: unknown): LogoutResponse {
  if (!isRecord(payload)) {
    throw new Error("Logout response was not an object.");
  }

  return {
    status: requireString(payload.status, "status"),
  };
}

/**
 * Purpose: Build a typed auth error from a non-success HTTP response body.
 * Inputs: HTTP status code and unknown JSON payload from the auth API.
 * Outputs: A structured `AuthApiError`.
 * Behavior: Handles FastAPI validation errors and canonical auth detail objects without losing the message.
 */
export function buildAuthApiError(statusCode: number, payload: unknown): AuthApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      const code = asAuthErrorCode(detail.code);
      const message = typeof detail.message === "string" ? detail.message : "Authentication failed.";
      return new AuthApiError({
        code,
        message,
        statusCode,
      });
    }

    if (Array.isArray(detail)) {
      return new AuthApiError({
        code: "validation_error",
        message: "Review the highlighted fields and try again.",
        statusCode,
      });
    }
  }

  return new AuthApiError({
    code: "unknown_error",
    message: "Authentication failed. Try again or reload the desktop workspace.",
    statusCode,
  });
}

async function authRequest<TResponse>(
  path: string,
  init: Readonly<RequestInit>,
): Promise<TResponse> {
  const response = await fetch(`${AUTH_PROXY_BASE_PATH}${path}`, {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  const payload = await parseJsonPayload(response);
  if (!response.ok) {
    throw buildAuthApiError(response.status, payload);
  }

  if (path === "/logout") {
    return parseLogoutResponse(payload) as TResponse;
  }

  return parseAuthSessionResponse(payload) as TResponse;
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

function asAuthErrorCode(value: unknown): AuthErrorCode {
  switch (value) {
    case "duplicate_email":
    case "invalid_credentials":
    case "session_expired":
    case "session_required":
    case "user_disabled":
    case "validation_error":
      return value;
    default:
      return "unknown_error";
  }
}

function requireBoolean(value: unknown, fieldName: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Auth contract field "${fieldName}" must be a boolean.`);
  }

  return value;
}

function requireNullableString(value: unknown, fieldName: string): string | null {
  if (value === null) {
    return null;
  }

  return requireString(value, fieldName);
}

function requireString(value: unknown, fieldName: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Auth contract field "${fieldName}" must be a non-empty string.`);
  }

  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
