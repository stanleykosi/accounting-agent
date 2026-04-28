/*
Purpose: Build canonical same-origin entity proxy paths for desktop UI API clients.
Scope: `/api/entities/**` URL composition with path-segment encoding.
Dependencies: Native URI encoding only.
*/

export const ENTITY_PROXY_BASE_PATH = "/api/entities";

export function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `${ENTITY_PROXY_BASE_PATH}/${encodedSegments.join("/")}`;
}
