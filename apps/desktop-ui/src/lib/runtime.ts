/*
Purpose: Centralize frontend runtime mode resolution for hosted and self-managed deployments.
Scope: Deployment-mode defaults, hosted/runtime guards, and small helpers shared by middleware and setup flows.
Dependencies: Process environment variables injected by Vercel, Tauri, or self-managed runtimes.
*/

export type FrontendRuntimeMode = "hosted" | "desktop-local";

/**
 * Purpose: Resolve the canonical frontend runtime mode for the current Next.js process.
 * Inputs: Process environment only.
 * Outputs: Either `hosted` for deployed web origins or `desktop-local` for self-managed sidecar flows.
 * Behavior: Prefers explicit configuration, defaults to self-managed mode during development, and hosted mode otherwise.
 */
export function resolveFrontendRuntimeMode(): FrontendRuntimeMode {
  const configuredMode = process.env.ACCOUNTING_AGENT_FRONTEND_MODE?.trim().toLowerCase();
  if (configuredMode === "desktop-local") {
    return "desktop-local";
  }

  if (configuredMode === "hosted") {
    return "hosted";
  }

  return process.env.NODE_ENV === "development" ? "desktop-local" : "hosted";
}

/**
 * Purpose: Tell callers whether the current frontend should skip loopback runtime gating entirely.
 * Inputs: None.
 * Outputs: True when the app runs as a hosted frontend for browser or remote desktop shells.
 * Behavior: Keeps all hosted-mode branching grounded in one explicit runtime helper.
 */
export function isHostedFrontendRuntime(): boolean {
  return resolveFrontendRuntimeMode() === "hosted";
}

/**
 * Purpose: Resolve the canonical backend API base URL for server-side proxying and session checks.
 * Inputs: Process environment only.
 * Outputs: A normalized backend API base URL without trailing slashes.
 * Behavior: Requires explicit configuration for hosted deployments and only falls back to loopback
 * in desktop-local mode.
 */
export function resolveBackendApiBaseUrl(): string {
  const configuredUrl = process.env.ACCOUNTING_AGENT_API_URL?.trim();
  if (configuredUrl && configuredUrl.length > 0) {
    return configuredUrl.replace(/\/+$/u, "");
  }

  if (resolveFrontendRuntimeMode() === "hosted") {
    throw new Error(
      "ACCOUNTING_AGENT_API_URL must be set for hosted frontend deployments.",
    );
  }

  return "http://127.0.0.1:8000/api";
}
