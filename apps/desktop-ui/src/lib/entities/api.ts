/*
Purpose: Centralize same-origin entity API helpers and backend URL resolution for the desktop UI.
Scope: Browser-safe entity fetch helpers, structured error handling, and backend proxy URL composition.
Dependencies: The generated TS SDK schema types and the Next.js same-origin entity proxy routes.
*/

import type { components } from "@accounting-ai-agent/ts-sdk";

export type CreateEntityRequest = components["schemas"]["CreateEntityRequest"];
export type CreateEntityMembershipRequest = components["schemas"]["CreateEntityMembershipRequest"];
export type EntityListResponse = components["schemas"]["EntityListResponse"];
export type EntitySummary = components["schemas"]["EntitySummary"];
export type EntityWorkspace = components["schemas"]["EntityWorkspace"];
export type UpdateEntityMembershipRequest = components["schemas"]["UpdateEntityMembershipRequest"];
export type UpdateEntityRequest = components["schemas"]["UpdateEntityRequest"];

export type EntityApiErrorCode =
  | "default_actor_required"
  | "duplicate_membership"
  | "entity_not_found"
  | "membership_not_found"
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

const ENTITY_PROXY_BASE_PATH = "/api/entities";

/**
 * Purpose: Build the backend FastAPI entity URL targeted by the Next.js proxy handlers.
 * Inputs: A route suffix under the backend `/entities` router and optional raw search params.
 * Outputs: A fully qualified backend entity API URL.
 * Behavior: Uses one canonical API base URL and avoids duplicate slashes during composition.
 */
export function buildBackendEntitiesUrl(path: string, search = ""): string {
  const normalizedBaseUrl = (
    process.env.ACCOUNTING_AGENT_API_URL ?? "http://127.0.0.1:8000/api"
  ).replace(/\/+$/u, "");
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
  return entityRequest<EntityWorkspace>(
    `${ENTITY_PROXY_BASE_PATH}/${encodeURIComponent(entityId)}`,
    {
      method: "GET",
    },
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
  return entityRequest<EntityWorkspace>(
    `${ENTITY_PROXY_BASE_PATH}/${encodeURIComponent(entityId)}`,
    {
      body: JSON.stringify(payload),
      method: "PATCH",
    },
  );
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
  return entityRequest<EntityWorkspace>(
    `${ENTITY_PROXY_BASE_PATH}/${encodeURIComponent(entityId)}/memberships`,
    {
      body: JSON.stringify(payload),
      method: "POST",
    },
  );
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
    `${ENTITY_PROXY_BASE_PATH}/${encodeURIComponent(entityId)}/memberships/${encodeURIComponent(membershipId)}`,
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
    case "membership_not_found":
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
