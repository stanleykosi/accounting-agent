/*
Purpose: Provide the generated TypeScript SDK entrypoint for local TypeScript clients.
Scope: Re-export generated OpenAPI types and expose a typed fetch client factory bound to the API schema.
Dependencies: openapi-fetch and the generated OpenAPI types under src/generated/openapi.ts.
*/

import createClient from "openapi-fetch";
import type { ClientOptions } from "openapi-fetch";

import type { components, operations, paths } from "./generated/openapi";

/**
 * Create a typed client for the Accounting AI Agent FastAPI surface.
 *
 * @param clientOptions - Optional fetch client configuration such as base URL and middleware.
 * @returns A typed OpenAPI fetch client bound to the generated API paths.
 */
export function createAccountingAgentClient(clientOptions: ClientOptions = {}) {
  return createClient<paths>(clientOptions);
}

/**
 * Resolve the canonical loopback API base URL used by local TypeScript clients.
 *
 * @param options - Optional host, port, and API base path overrides for local environments.
 * @returns The normalized loopback base URL expected by the generated client.
 */
export function resolveLoopbackApiBaseUrl(
  options: {
    apiBasePath?: string;
    host?: string;
    port?: number;
  } = {},
): string {
  const apiBasePath = normalizeApiBasePath(options.apiBasePath ?? "/api");
  const host = options.host ?? "127.0.0.1";
  const port = options.port ?? 8000;

  return `http://${host}:${port}${apiBasePath}`;
}

/**
 * Normalize a configurable API base path into a canonical leading-slash form.
 *
 * @param apiBasePath - The raw base path value supplied by a caller or environment config.
 * @returns A normalized base path safe for URL composition.
 */
export function normalizeApiBasePath(apiBasePath: string): string {
  const trimmedPath = apiBasePath.trim();
  if (!trimmedPath) {
    throw new Error("The API base path cannot be empty.");
  }

  const normalizedPath = `/${trimmedPath.replace(/^\/+/, "")}`;
  return normalizedPath === "/" ? normalizedPath : normalizedPath.replace(/\/+$/, "");
}

export type { ClientOptions, components, operations, paths };
export type AccountingAgentApiClient = ReturnType<typeof createAccountingAgentClient>;
