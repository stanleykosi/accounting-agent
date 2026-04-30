/*
Purpose: Centralize same-origin entity API helpers and backend URL resolution for the desktop UI.
Scope: Browser-safe entity fetch helpers, structured error handling, and backend proxy URL composition.
Dependencies: The generated TS SDK schema types and the Next.js same-origin entity proxy routes.
*/

import type { components } from "@accounting-ai-agent/ts-sdk";
import {
  buildEntityCacheInvalidationPrefixes,
  invalidateClientCacheByPrefix,
  loadClientCachedValue,
  readValidatedClientCacheSnapshot,
} from "../client-cache";
import { buildEntityProxyPath, ENTITY_PROXY_BASE_PATH } from "../entity-proxy";
import { resolveBackendApiBaseUrl } from "../runtime";

export type CreateEntityRequest = components["schemas"]["CreateEntityRequest"];
export type CreateEntityMembershipRequest = components["schemas"]["CreateEntityMembershipRequest"];
export type EntityListResponse = components["schemas"]["EntityListResponse"];
export type EntitySummary = components["schemas"]["EntitySummary"];
export type EntityWorkspace = components["schemas"]["EntityWorkspace"];
export type UpdateEntityMembershipRequest = components["schemas"]["UpdateEntityMembershipRequest"];
export type UpdateEntityRequest = components["schemas"]["UpdateEntityRequest"];
export type EntityDeleteResponse = {
  canceled_job_count: number;
  deleted_close_run_count: number;
  deleted_document_count: number;
  deleted_entity_id: string;
  deleted_entity_name: string;
  deleted_thread_count: number;
};

export type EntityApiErrorCode =
  | "default_actor_required"
  | "duplicate_membership"
  | "entity_not_found"
  | "integrity_conflict"
  | "membership_not_found"
  | "owner_required"
  | "session_expired"
  | "session_required"
  | "unknown_error"
  | "user_disabled"
  | "user_not_found"
  | "validation_error";

/**
 * Purpose: Represent a structured entity API failure that UI callers can branch on safely.
 * Inputs: Stable error code, HTTP status code, and an operator-facing message from the API boundary.
 * Outputs: A typed Error instance used across entity fetch and mutation flows.
 * Behavior: Preserves the original server message while attaching stable machine-readable metadata.
 */
export class EntityApiError extends Error {
  readonly code: EntityApiErrorCode;
  readonly statusCode: number;

  constructor(
    options: Readonly<{ code: EntityApiErrorCode; message: string; statusCode: number }>,
  ) {
    super(options.message);
    this.name = "EntityApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

const ENTITY_READ_CACHE_TTL_MS = 30_000;

/**
 * Purpose: Build the backend FastAPI entity URL targeted by the Next.js proxy handlers.
 * Inputs: A route suffix under the backend `/entities` router and optional raw search params.
 * Outputs: A fully qualified backend entity API URL.
 * Behavior: Uses one canonical API base URL and avoids duplicate slashes during composition.
 */
export function buildBackendEntitiesUrl(path: string, search = ""): string {
  const normalizedBaseUrl = resolveBackendApiBaseUrl();
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedSearch = search.startsWith("?") || search.length === 0 ? search : `?${search}`;
  const entityPathSuffix = normalizedPath === "/" ? "" : normalizedPath;
  return `${normalizedBaseUrl}/entities${entityPathSuffix}${normalizedSearch}`;
}

/**
 * Purpose: Load the authenticated operator's entity workspace list through the same-origin proxy.
 * Inputs: None.
 * Outputs: The typed entity list response returned by the backend API.
 * Behavior: Uses `no-store` requests so workspace state stays current after mutations.
 */
export async function listEntities(): Promise<EntityListResponse> {
  return entityRequest<EntityListResponse>(ENTITY_PROXY_BASE_PATH, {
    method: "GET",
  });
}

export function readEntityListSnapshot(): EntityListResponse | null {
  return readValidatedClientCacheSnapshot<EntityListResponse>(
    ENTITY_PROXY_BASE_PATH,
    isValidEntityListResponse,
  );
}

/**
 * Purpose: Create a new entity workspace through the same-origin proxy.
 * Inputs: The workspace fields required by the backend create contract.
 * Outputs: The hydrated entity workspace returned by the backend.
 * Behavior: Sends JSON to the proxy so browser cookies and session rotation remain same-origin.
 */
export async function createEntity(
  payload: Readonly<CreateEntityRequest>,
): Promise<EntityWorkspace> {
  return entityRequest<EntityWorkspace>(ENTITY_PROXY_BASE_PATH, {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Load one entity workspace detail view through the same-origin proxy.
 * Inputs: The UUID of the entity workspace to read.
 * Outputs: The full workspace response including memberships and activity history.
 * Behavior: Encodes the path segment so malformed IDs cannot break route composition.
 */
export async function readEntityWorkspace(entityId: string): Promise<EntityWorkspace> {
  return entityRequest<EntityWorkspace>(buildEntityProxyPath(entityId, []), {
    method: "GET",
  });
}

export function readEntityWorkspaceSnapshot(entityId: string): EntityWorkspace | null {
  return readValidatedClientCacheSnapshot<EntityWorkspace>(
    buildEntityProxyPath(entityId, []),
    isValidEntityWorkspace,
  );
}

/**
 * Purpose: Update one entity workspace through the same-origin proxy.
 * Inputs: The workspace UUID and the partial update payload supported by the backend.
 * Outputs: The refreshed entity workspace after the mutation succeeds.
 * Behavior: Preserves browser cookie rotation by routing the PATCH through Next.js.
 */
export async function updateEntity(
  entityId: string,
  payload: Readonly<UpdateEntityRequest>,
): Promise<EntityWorkspace> {
  return entityRequest<EntityWorkspace>(buildEntityProxyPath(entityId, []), {
    body: JSON.stringify(payload),
    method: "PATCH",
  });
}

/**
 * Purpose: Delete one entity workspace through the same-origin proxy.
 * Inputs: The workspace UUID that should be deleted irreversibly.
 * Outputs: Structured delete outcome including deleted close-run and document counts.
 * Behavior: Uses DELETE so the backend can run the canonical owner-only destructive workflow.
 */
export async function deleteEntityWorkspace(entityId: string): Promise<EntityDeleteResponse> {
  return entityRequest<EntityDeleteResponse>(buildEntityProxyPath(entityId, []), {
    method: "DELETE",
  });
}

/**
 * Purpose: Add an existing local operator to a workspace through the same-origin proxy.
 * Inputs: The entity UUID and the membership create payload expected by the backend.
 * Outputs: The refreshed entity workspace after the membership is created.
 * Behavior: Leaves all auth handling to the same-origin proxy so the browser stays synchronized.
 */
export async function createEntityMembership(
  entityId: string,
  payload: Readonly<CreateEntityMembershipRequest>,
): Promise<EntityWorkspace> {
  return entityRequest<EntityWorkspace>(buildEntityProxyPath(entityId, ["memberships"]), {
    body: JSON.stringify(payload),
    method: "POST",
  });
}

/**
 * Purpose: Update one workspace membership through the same-origin proxy.
 * Inputs: The entity UUID, membership UUID, and partial update payload.
 * Outputs: The refreshed entity workspace after the membership update succeeds.
 * Behavior: Uses PATCH so the backend can distinguish targeted membership updates from creation flows.
 */
export async function updateEntityMembership(
  entityId: string,
  membershipId: string,
  payload: Readonly<UpdateEntityMembershipRequest>,
): Promise<EntityWorkspace> {
  return entityRequest<EntityWorkspace>(
    buildEntityProxyPath(entityId, ["memberships", membershipId]),
    {
      body: JSON.stringify(payload),
      method: "PATCH",
    },
  );
}

async function entityRequest<TResponse>(
  path: string,
  init: Readonly<RequestInit>,
): Promise<TResponse> {
  const requestMethod = normalizeRequestMethod(init.method);

  const performRequest = async (): Promise<TResponse> => {
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
      throw buildEntityApiError(response.status, payload);
    }

    return payload as TResponse;
  };

  if (requestMethod === "GET") {
    return loadClientCachedValue(path, performRequest, ENTITY_READ_CACHE_TTL_MS, {
      isValid: (payload) => isValidEntityCachePayload(path, payload),
    });
  }

  const payload = await performRequest();
  invalidateClientCacheByPrefix(buildEntityCacheInvalidationPrefixes(path));
  return payload;
}

function buildEntityApiError(statusCode: number, payload: unknown): EntityApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      const code = asEntityApiErrorCode(detail.code);
      const message =
        typeof detail.message === "string"
          ? detail.message
          : "The entity workspace request could not be completed.";
      return new EntityApiError({
        code,
        message,
        statusCode,
      });
    }

    if (Array.isArray(detail)) {
      return new EntityApiError({
        code: "validation_error",
        message: "Review the highlighted entity workspace fields and try again.",
        statusCode,
      });
    }
  }

  return new EntityApiError({
    code: "unknown_error",
    message: "The entity workspace request failed. Reload the page and try again.",
    statusCode,
  });
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

function asEntityApiErrorCode(value: unknown): EntityApiErrorCode {
  switch (value) {
    case "default_actor_required":
    case "duplicate_membership":
    case "entity_not_found":
    case "integrity_conflict":
    case "membership_not_found":
    case "owner_required":
    case "session_expired":
    case "session_required":
    case "user_disabled":
    case "user_not_found":
      return value;
    default:
      return "unknown_error";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isValidEntityCachePayload(path: string, payload: unknown): boolean {
  const pathname = path.split("?")[0] ?? path;
  if (pathname === ENTITY_PROXY_BASE_PATH) {
    return isValidEntityListResponse(payload);
  }
  return isValidEntityWorkspace(payload);
}

function isValidEntityListResponse(value: unknown): value is EntityListResponse {
  if (!isRecord(value) || !Array.isArray(value.entities)) {
    return false;
  }
  return value.entities.every(isValidEntitySummary);
}

function isValidEntityWorkspace(value: unknown): value is EntityWorkspace {
  if (!isValidEntitySummary(value) || !isRecord(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  const memberships = record.memberships;
  const activity = record.activity;
  return (
    (memberships === undefined || Array.isArray(memberships)) &&
    (activity === undefined || Array.isArray(activity))
  );
}

function isValidEntitySummary(value: unknown): value is EntitySummary {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.name === "string" &&
    (typeof value.legal_name === "string" || value.legal_name === null) &&
    typeof value.base_currency === "string" &&
    typeof value.country_code === "string" &&
    typeof value.timezone === "string" &&
    typeof value.status === "string" &&
    typeof value.member_count === "number"
  );
}

function normalizeRequestMethod(value: string | null | undefined): string {
  return (value ?? "GET").toUpperCase();
}
