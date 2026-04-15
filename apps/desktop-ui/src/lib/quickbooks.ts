/*
Purpose: Centralize same-origin QuickBooks integration API helpers for the desktop UI.
Scope: Connection status, OAuth redirect creation, disconnect, and chart-of-accounts sync calls.
Dependencies: Browser Fetch APIs and the existing `/api/entities/**` proxy surface.
*/

export type QuickBooksConnectionStatus =
  | "connected"
  | "disconnected"
  | "error"
  | "expired"
  | "revoked";

export type QuickBooksConnectionStatusResponse = {
  external_realm_id: string | null;
  last_sync_at: string | null;
  recovery_action: string | null;
  status: QuickBooksConnectionStatus;
};

export type QuickBooksConnectResponse = {
  authorization_url: string;
};

export type QuickBooksDisconnectResponse = {
  message: string;
  status: QuickBooksConnectionStatus;
};

export type QuickBooksCoaSyncResponse = {
  account_count: number;
  activated: boolean;
  coa_set_id: string;
  message: string;
  synced_at: string;
  version_no: number;
};

export type QuickBooksApiErrorCode =
  | "entity_not_found"
  | "quickbooks_oauth_not_configured"
  | "quickbooks_reauthorization_required"
  | "quickbooks_sync_failed"
  | "session_expired"
  | "session_required"
  | "unknown_error"
  | "user_disabled"
  | "validation_error";

/**
 * Purpose: Represent structured QuickBooks API failures that UI callers can branch on safely.
 * Inputs: Stable backend error code, HTTP status code, and operator-facing message.
 * Outputs: Typed Error instances with both human and machine-readable diagnostics.
 * Behavior: Keeps QuickBooks recovery copy consistent across status and sync flows.
 */
export class QuickBooksApiError extends Error {
  readonly code: QuickBooksApiErrorCode;
  readonly statusCode: number;

  constructor(
    options: Readonly<{ code: QuickBooksApiErrorCode; message: string; statusCode: number }>,
  ) {
    super(options.message);
    this.name = "QuickBooksApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

const ENTITY_PROXY_BASE_PATH = "/api/entities";

/**
 * Purpose: Read sanitized QuickBooks connection status for one entity workspace.
 * Inputs: Entity UUID from route context.
 * Outputs: Connection lifecycle status and recovery guidance.
 * Behavior: Uses same-origin proxying so rotated auth cookies remain synchronized.
 */
export async function readQuickBooksStatus(
  entityId: string,
): Promise<QuickBooksConnectionStatusResponse> {
  return quickBooksRequest<QuickBooksConnectionStatusResponse>(
    buildEntityProxyPath(entityId, ["integrations", "quickbooks", "status"]),
    { method: "GET" },
  );
}

/**
 * Purpose: Create a QuickBooks authorization redirect URL for one entity.
 * Inputs: Entity UUID and local UI return URL.
 * Outputs: QuickBooks authorization URL for top-level browser navigation.
 * Behavior: Backend signs the return URL into OAuth state before returning the redirect target.
 */
export async function startQuickBooksConnection(
  entityId: string,
  returnUrl: string,
): Promise<QuickBooksConnectResponse> {
  const searchParams = new URLSearchParams({ return_url: returnUrl });
  return quickBooksRequest<QuickBooksConnectResponse>(
    `${buildEntityProxyPath(entityId, ["integrations", "quickbooks", "connect"])}?${searchParams}`,
    { method: "GET" },
  );
}

/**
 * Purpose: Revoke local QuickBooks connection state for one entity.
 * Inputs: Entity UUID.
 * Outputs: Resulting connection status and operator-facing message.
 * Behavior: Backend attempts remote token revocation and always marks local state as revoked.
 */
export async function disconnectQuickBooks(
  entityId: string,
): Promise<QuickBooksDisconnectResponse> {
  return quickBooksRequest<QuickBooksDisconnectResponse>(
    buildEntityProxyPath(entityId, ["integrations", "quickbooks", "disconnect"]),
    { method: "POST" },
  );
}

/**
 * Purpose: Synchronize QuickBooks chart-of-accounts accounts into a versioned COA set.
 * Inputs: Entity UUID.
 * Outputs: Created COA set metadata and account count.
 * Behavior: Keeps direct posting out of scope while enabling provider-aware COA and posting-package flows.
 */
export async function syncQuickBooksCoa(entityId: string): Promise<QuickBooksCoaSyncResponse> {
  return quickBooksRequest<QuickBooksCoaSyncResponse>(
    buildEntityProxyPath(entityId, ["integrations", "quickbooks", "sync-coa"]),
    { method: "POST" },
  );
}

async function quickBooksRequest<TResponse>(
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
    throw buildQuickBooksApiError(response.status, payload);
  }
  return payload as TResponse;
}

function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `${ENTITY_PROXY_BASE_PATH}/${encodedSegments.join("/")}`;
}

function buildQuickBooksApiError(statusCode: number, payload: unknown): QuickBooksApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      return new QuickBooksApiError({
        code: asQuickBooksApiErrorCode(detail.code),
        message:
          typeof detail.message === "string"
            ? detail.message
            : "The QuickBooks request could not be completed.",
        statusCode,
      });
    }
    if (Array.isArray(detail)) {
      return new QuickBooksApiError({
        code: "validation_error",
        message: "Review the QuickBooks request fields and try again.",
        statusCode,
      });
    }
  }

  return new QuickBooksApiError({
    code: "unknown_error",
    message: "The QuickBooks request failed. Reload and try again.",
    statusCode,
  });
}

function asQuickBooksApiErrorCode(value: unknown): QuickBooksApiErrorCode {
  switch (value) {
    case "entity_not_found":
    case "quickbooks_oauth_not_configured":
    case "quickbooks_reauthorization_required":
    case "quickbooks_sync_failed":
    case "session_expired":
    case "session_required":
    case "user_disabled":
      return value;
    default:
      return "unknown_error";
  }
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }
  return response.json();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
