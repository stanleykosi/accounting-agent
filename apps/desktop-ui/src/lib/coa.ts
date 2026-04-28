/*
Purpose: Centralize same-origin chart-of-accounts API helpers for the desktop UI.
Scope: COA workspace reads, manual upload calls, set activation, and account editor mutations.
Dependencies: Browser Fetch APIs and the existing `/api/entities/**` proxy surface.
*/

import { buildEntityProxyPath } from "./entity-proxy";

export type CoaSetSource = "manual_upload" | "quickbooks_sync" | "fallback_nigerian_sme";

export type CoaSetSummary = {
  account_count: number;
  activated_at: string | null;
  created_at: string;
  entity_id: string;
  id: string;
  import_metadata: Record<string, unknown>;
  is_active: boolean;
  source: CoaSetSource;
  updated_at: string;
  version_no: number;
};

export type CoaAccountSummary = {
  account_code: string;
  account_name: string;
  account_type: string;
  coa_set_id: string;
  created_at: string;
  dimension_defaults: Record<string, string>;
  external_ref: string | null;
  id: string;
  is_active: boolean;
  is_postable: boolean;
  parent_account_id: string | null;
  updated_at: string;
};

export type CoaWorkspaceResponse = {
  accounts: readonly CoaAccountSummary[];
  active_set: CoaSetSummary;
  coa_sets: readonly CoaSetSummary[];
  entity_id: string;
  precedence_order: readonly CoaSetSource[];
};

export type CoaCreateAccountRequest = {
  account_code: string;
  account_name: string;
  account_type: string;
  dimension_defaults?: Record<string, string>;
  external_ref?: string;
  is_active?: boolean;
  is_postable?: boolean;
  parent_account_id?: string;
};

export type CoaUpdateAccountRequest = Partial<
  Pick<
    CoaCreateAccountRequest,
    | "account_code"
    | "account_name"
    | "account_type"
    | "dimension_defaults"
    | "external_ref"
    | "is_active"
    | "is_postable"
    | "parent_account_id"
  >
>;

export type CoaApiErrorCode =
  | "coa_account_not_found"
  | "coa_set_not_found"
  | "duplicate_account_code"
  | "entity_archived"
  | "entity_not_found"
  | "integrity_conflict"
  | "invalid_coa_file"
  | "invalid_parent_account"
  | "session_expired"
  | "session_required"
  | "stale_account"
  | "unknown_error"
  | "unsupported_file_type"
  | "user_disabled"
  | "validation_error";

/**
 * Purpose: Represent structured COA API failures that UI callers can branch on safely.
 * Inputs: Stable error code, HTTP status code, and operator-facing message from backend routes.
 * Outputs: Typed Error instances with both human and machine-readable diagnostics.
 * Behavior: Preserves fail-fast API messages while maintaining stable code branches for UI behavior.
 */
export class CoaApiError extends Error {
  readonly code: CoaApiErrorCode;
  readonly statusCode: number;

  constructor(options: Readonly<{ code: CoaApiErrorCode; message: string; statusCode: number }>) {
    super(options.message);
    this.name = "CoaApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

/**
 * Purpose: Read one entity COA workspace through the same-origin proxy.
 * Inputs: Entity UUID from route context.
 * Outputs: Active COA set, account rows, and set history metadata.
 * Behavior: Uses `no-store` fetch semantics so activation/edit updates appear immediately.
 */
export async function readCoaWorkspace(entityId: string): Promise<CoaWorkspaceResponse> {
  return coaRequest<CoaWorkspaceResponse>(buildEntityProxyPath(entityId, ["coa"]), {
    method: "GET",
  });
}

/**
 * Purpose: Upload a manual COA file as a new active version through the same-origin proxy.
 * Inputs: Entity UUID and browser-selected COA file.
 * Outputs: Refreshed COA workspace payload after import and activation.
 * Behavior: Uses FormData so the backend can validate raw upload bytes and file names.
 */
export async function uploadManualCoa(entityId: string, file: File): Promise<CoaWorkspaceResponse> {
  const formData = new FormData();
  formData.append("file", file);

  return coaRequest<CoaWorkspaceResponse>(buildEntityProxyPath(entityId, ["coa", "upload"]), {
    body: formData,
    method: "POST",
  });
}

/**
 * Purpose: Activate a chosen COA set version for an entity workspace.
 * Inputs: Entity UUID, target COA set UUID, and optional operator reason.
 * Outputs: Refreshed COA workspace payload with the activated set.
 * Behavior: Sends JSON payload through same-origin proxy to preserve cookie/session handling.
 */
export async function activateCoaSet(options: {
  entityId: string;
  coaSetId: string;
  reason?: string;
}): Promise<CoaWorkspaceResponse> {
  return coaRequest<CoaWorkspaceResponse>(
    buildEntityProxyPath(options.entityId, ["coa", "sets", options.coaSetId, "activate"]),
    {
      body: JSON.stringify({ reason: options.reason }),
      method: "POST",
    },
  );
}

/**
 * Purpose: Create a new COA account using versioned edit semantics.
 * Inputs: Entity UUID and account payload fields.
 * Outputs: Refreshed COA workspace payload with a new active set version.
 * Behavior: Delegates canonical validation and revision materialization to backend service rules.
 */
export async function createCoaAccount(
  entityId: string,
  payload: Readonly<CoaCreateAccountRequest>,
): Promise<CoaWorkspaceResponse> {
  return coaRequest<CoaWorkspaceResponse>(buildEntityProxyPath(entityId, ["coa", "accounts"]), {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Update one COA account using immutable versioned revision rules.
 * Inputs: Entity UUID, account UUID from active set, and partial update payload.
 * Outputs: Refreshed COA workspace payload with the new active set version.
 * Behavior: Sends PATCH payload through same-origin proxy to keep browser auth state synchronized.
 */
export async function updateCoaAccount(
  entityId: string,
  accountId: string,
  payload: Readonly<CoaUpdateAccountRequest>,
): Promise<CoaWorkspaceResponse> {
  return coaRequest<CoaWorkspaceResponse>(
    buildEntityProxyPath(entityId, ["coa", "accounts", accountId]),
    {
      body: JSON.stringify(payload),
      method: "PATCH",
    },
  );
}

async function coaRequest<TResponse>(
  path: string,
  init: Readonly<RequestInit>,
): Promise<TResponse> {
  const isFormDataBody = init.body instanceof FormData;
  const response = await fetch(path, {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init.body && !isFormDataBody ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  const payload = await parseJsonPayload(response);
  if (!response.ok) {
    throw buildCoaApiError(response.status, payload);
  }

  return payload as TResponse;
}

function buildCoaApiError(statusCode: number, payload: unknown): CoaApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      const message =
        typeof detail.message === "string"
          ? detail.message
          : "The chart-of-accounts request could not be completed.";
      return new CoaApiError({
        code: asCoaApiErrorCode(detail.code),
        message,
        statusCode,
      });
    }

    if (Array.isArray(detail)) {
      return new CoaApiError({
        code: "validation_error",
        message: "Review the chart-of-accounts input fields and try again.",
        statusCode,
      });
    }
  }

  return new CoaApiError({
    code: "unknown_error",
    message: "The chart-of-accounts request failed. Reload the page and try again.",
    statusCode,
  });
}

function asCoaApiErrorCode(value: unknown): CoaApiErrorCode {
  switch (value) {
    case "coa_account_not_found":
    case "coa_set_not_found":
    case "duplicate_account_code":
    case "entity_archived":
    case "entity_not_found":
    case "integrity_conflict":
    case "invalid_coa_file":
    case "invalid_parent_account":
    case "session_expired":
    case "session_required":
    case "stale_account":
    case "unsupported_file_type":
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
