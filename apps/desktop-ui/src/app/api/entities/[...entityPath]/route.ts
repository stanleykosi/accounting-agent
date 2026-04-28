/*
Purpose: Proxy same-origin entity detail, update, and membership requests to the FastAPI backend.
Scope: GET, POST, PATCH, PUT, and DELETE forwarding for all `/api/entities/**` routes.
Dependencies: The shared entity proxy helper and Next.js route handlers.
*/

import { proxyEntityRequest } from "../../../../lib/entities/proxy";

type EntityProxyRouteContext = {
  params: Promise<{
    entityPath: string[];
  }>;
};

/**
 * Purpose: Proxy nested entity GET requests such as workspace-detail reads to the backend.
 * Inputs: The incoming request and resolved entity-path segments.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Uses one shared proxy helper so nested entity routes cannot drift from the base route behavior.
 */
export async function GET(request: Request, context: EntityProxyRouteContext): Promise<Response> {
  const { entityPath } = await context.params;
  return proxyEntityRequest(request, entityPath);
}

/**
 * Purpose: Proxy nested entity POST requests such as membership creation to the backend.
 * Inputs: The incoming request and resolved entity-path segments.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Leaves all entity-side validation and mutation logic in the canonical FastAPI service.
 */
export async function POST(request: Request, context: EntityProxyRouteContext): Promise<Response> {
  const { entityPath } = await context.params;
  return proxyEntityRequest(request, entityPath);
}

/**
 * Purpose: Proxy nested entity PATCH requests such as workspace and membership updates to the backend.
 * Inputs: The incoming request and resolved entity-path segments.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Keeps patch semantics and rotated session cookies intact for browser-originated updates.
 */
export async function PATCH(request: Request, context: EntityProxyRouteContext): Promise<Response> {
  const { entityPath } = await context.params;
  return proxyEntityRequest(request, entityPath);
}

/**
 * Purpose: Proxy nested entity PUT requests such as report commentary replacements to the backend.
 * Inputs: The incoming request and resolved entity-path segments.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Ensures PUT-only commentary endpoints reach the FastAPI backend through the same proxy.
 */
export async function PUT(request: Request, context: EntityProxyRouteContext): Promise<Response> {
  const { entityPath } = await context.params;
  return proxyEntityRequest(request, entityPath);
}

/**
 * Purpose: Proxy nested entity DELETE requests such as resource removals to the backend.
 * Inputs: The incoming request and resolved entity-path segments.
 * Outputs: The backend response with status, JSON, and cookies preserved.
 * Behavior: Keeps deletion semantics and rotated session cookies intact for browser-originated deletes.
 */
export async function DELETE(
  request: Request,
  context: EntityProxyRouteContext,
): Promise<Response> {
  const { entityPath } = await context.params;
  return proxyEntityRequest(request, entityPath);
}
