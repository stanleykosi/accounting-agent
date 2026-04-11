/*
Purpose: Centralize same-origin ownership API helpers for the desktop UI.
Scope: Browser-safe lock acquire/release/touch helpers, backend URL composition,
and structured error handling for ownership collisions.
Dependencies: Standard Fetch APIs and the Next.js same-origin ownership proxy route.
*/

export type OwnershipTargetType =
  | "entity"
  | "close_run"
  | "document"
  | "recommendation"
  | "review_target";

export type OwnershipOperatorSummary = {
  email: string;
  full_name: string;
  id: string;
};

export type OwnershipTargetReference = {
  close_run_id?: string | null;
  target_id: string;
  target_type: OwnershipTargetType;
};

export type AcquireOwnershipLockRequest = OwnershipTargetReference & {
  note?: string | null;
  owner_user_id?: string | null;
};

export type ReleaseOwnershipLockRequest = OwnershipTargetReference;

export type TouchOwnershipTargetRequest = OwnershipTargetReference;

export type OwnershipState = {
  close_run_id: string | null;
  entity_id: string;
  last_touched_at: string | null;
  last_touched_by: OwnershipOperatorSummary | null;
  lock_note: string | null;
  locked_at: string | null;
  locked_by: OwnershipOperatorSummary | null;
  owner: OwnershipOperatorSummary | null;
  target_id: string;
  target_type: OwnershipTargetType;
};

export type OwnershipApiErrorCode =
  | "close_run_not_found"
  | "entity_not_found"
  | "integrity_conflict"
  | "lock_conflict"
  | "lock_not_held"
  | "owner_not_found"
  | "session_expired"
  | "session_required"
  | "target_not_found"
  | "target_scope_invalid"
  | "unknown_error"
  | "user_disabled"
  | "validation_error";

/**
 * Purpose: Represent a structured ownership API failure that UI callers can branch on safely.
 * Inputs: Stable error code, HTTP status code, and operator-facing API message.
 * Outputs: A typed Error instance for lock and last-touch flows.
 * Behavior: Preserves server diagnostics so collision recovery stays explicit.
 */
export class OwnershipApiError extends Error {
  readonly code: OwnershipApiErrorCode;
  readonly statusCode: number;

  constructor(
    options: Readonly<{ code: OwnershipApiErrorCode; message: string; statusCode: number }>,
  ) {
    super(options.message);
    this.name = "OwnershipApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

const OWNERSHIP_PROXY_BASE_PATH = "/api/ownership";

/**
 * Purpose: Build the backend FastAPI ownership URL targeted by the Next.js proxy.
 * Inputs: A route suffix under `/entities/{entityId}/ownership` and optional search params.
 * Outputs: A fully qualified backend ownership API URL.
 * Behavior: Uses one canonical backend API base URL and avoids duplicate slash composition.
 */
export function buildBackendOwnershipUrl(entityId: string, path: string, search = ""): string {
  const normalizedBaseUrl = (
    process.env.ACCOUNTING_AGENT_API_URL ?? "http://127.0.0.1:8000/api"
  ).replace(/\/+$/u, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedSearch = search.startsWith("?") || search.length === 0 ? search : `?${search}`;
  const ownershipPathSuffix = normalizedPath === "/" ? "" : normalizedPath;
  return `${normalizedBaseUrl}/entities/${encodeURIComponent(entityId)}/ownership${ownershipPathSuffix}${normalizedSearch}`;
}

/**
 * Purpose: Load ownership metadata for one target through the same-origin proxy.
 * Inputs: Entity UUID and target identity.
 * Outputs: The current ownership state returned by FastAPI.
 * Behavior: Carries close-run scope as a query parameter when the target needs it.
 */
export async function readOwnershipState(
  entityId: string,
  target: Readonly<OwnershipTargetReference>,
): Promise<OwnershipState> {
  const searchParams = new URLSearchParams();
  if (target.close_run_id !== undefined && target.close_run_id !== null) {
    searchParams.set("close_run_id", target.close_run_id);
  }

  return ownershipRequest<OwnershipState>(
    buildOwnershipProxyPath(
      entityId,
      ["targets", target.target_type, target.target_id],
      searchParams,
    ),
    { method: "GET" },
  );
}

/**
 * Purpose: Acquire an in-progress lock for one target through the same-origin proxy.
 * Inputs: Entity UUID and lock request payload.
 * Outputs: The refreshed ownership state after the lock is acquired.
 * Behavior: Lets the backend enforce member scope and collision checks.
 */
export async function acquireOwnershipLock(
  entityId: string,
  payload: Readonly<AcquireOwnershipLockRequest>,
): Promise<OwnershipState> {
  return ownershipRequest<OwnershipState>(buildOwnershipProxyPath(entityId, ["locks", "acquire"]), {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Release the current operator's lock for one target.
 * Inputs: Entity UUID and target reference payload.
 * Outputs: The refreshed ownership state with the lock cleared.
 * Behavior: Fails explicitly when the current operator does not hold the lock.
 */
export async function releaseOwnershipLock(
  entityId: string,
  payload: Readonly<ReleaseOwnershipLockRequest>,
): Promise<OwnershipState> {
  return ownershipRequest<OwnershipState>(buildOwnershipProxyPath(entityId, ["locks", "release"]), {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Record the current operator as the last touch for one target.
 * Inputs: Entity UUID and target reference payload.
 * Outputs: The refreshed ownership state.
 * Behavior: Refuses to touch a target currently locked by another operator.
 */
export async function touchOwnershipTarget(
  entityId: string,
  payload: Readonly<TouchOwnershipTargetRequest>,
): Promise<OwnershipState> {
  return ownershipRequest<OwnershipState>(buildOwnershipProxyPath(entityId, ["touch"]), {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Compose same-origin ownership proxy paths from trusted route segments.
 * Inputs: Entity UUID, route path segments, and optional query parameters.
 * Outputs: A browser-relative proxy URL.
 * Behavior: Percent-encodes every path segment so UUIDs and target types cannot alter routing.
 */
function buildOwnershipProxyPath(
  entityId: string,
  pathSegments: readonly string[],
  searchParams?: URLSearchParams,
): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  const query = searchParams !== undefined && searchParams.size > 0 ? `?${searchParams}` : "";
  return `${OWNERSHIP_PROXY_BASE_PATH}/${encodedSegments.join("/")}${query}`;
}

/**
 * Purpose: Execute a JSON ownership request through the same-origin proxy.
 * Inputs: Proxy path and Fetch initialization options.
 * Outputs: A typed response payload or structured OwnershipApiError.
 * Behavior: Preserves same-origin credentials and rejects non-OK responses consistently.
 */
async function ownershipRequest<TResponse>(
  path: string,
  init: Readonly<RequestInit>,
): Promise<TResponse> {
  const response = await fetch(path, {
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
    throw buildOwnershipApiError(response.status, payload);
  }

  return payload as TResponse;
}

/**
 * Purpose: Convert backend ownership failures into structured UI errors.
 * Inputs: HTTP status code and parsed response payload.
 * Outputs: OwnershipApiError with stable machine-readable code.
 * Behavior: Handles canonical API detail objects, FastAPI validation arrays, and unknown failures.
 */
function buildOwnershipApiError(statusCode: number, payload: unknown): OwnershipApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      const message =
        typeof detail.message === "string"
          ? detail.message
          : "The ownership request could not be completed.";
      return new OwnershipApiError({
        code: asOwnershipApiErrorCode(detail.code),
        message,
        statusCode,
      });
    }

    if (Array.isArray(detail)) {
      return new OwnershipApiError({
        code: "validation_error",
        message: "Review the ownership target details and try again.",
        statusCode,
      });
    }
  }

  return new OwnershipApiError({
    code: "unknown_error",
    message: "The ownership request failed. Reload the page and try again.",
    statusCode,
  });
}

/**
 * Purpose: Parse a response body only when the server declared JSON content.
 * Inputs: Fetch Response object.
 * Outputs: Parsed JSON payload or null for non-JSON responses.
 * Behavior: Avoids throwing on empty or non-JSON backend responses.
 */
async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

/**
 * Purpose: Normalize unknown backend error codes into the UI's supported ownership code union.
 * Inputs: A raw value from the backend detail payload.
 * Outputs: A stable OwnershipApiErrorCode value.
 * Behavior: Unknown values collapse to `unknown_error` for explicit fallback rendering.
 */
function asOwnershipApiErrorCode(value: unknown): OwnershipApiErrorCode {
  switch (value) {
    case "close_run_not_found":
    case "entity_not_found":
    case "integrity_conflict":
    case "lock_conflict":
    case "lock_not_held":
    case "owner_not_found":
    case "session_expired":
    case "session_required":
    case "target_not_found":
    case "target_scope_invalid":
    case "user_disabled":
      return value;
    default:
      return "unknown_error";
  }
}

/**
 * Purpose: Narrow unknown values to object records for safe payload inspection.
 * Inputs: Any JavaScript value.
 * Outputs: Type predicate indicating a non-null object record.
 * Behavior: Excludes null while accepting plain API response objects.
 */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
