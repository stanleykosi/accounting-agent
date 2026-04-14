/*
Purpose: Centralize hosted-vs-local frontend runtime mode resolution for web and desktop shells.
Scope: Deployment-mode defaults, hosted/runtime guards, and small helpers shared by middleware and setup flows.
Dependencies: Process environment variables injected by Vercel, Tauri, or local development shells.
*/

export type FrontendRuntimeMode = "hosted" | "desktop-local";

/**
 * Purpose: Resolve the canonical frontend runtime mode for the current Next.js process.
 * Inputs: Process environment only.
 * Outputs: Either `hosted` for Vercel/remote desktop shells or `desktop-local` for local sidecar flows.
 * Behavior: Prefers explicit configuration, defaults to local mode during development, and hosted mode otherwise.
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
 * Purpose: Tell callers whether the current frontend should skip local runtime gating entirely.
 * Inputs: None.
 * Outputs: True when the app runs as a hosted frontend for browser or remote desktop shells.
 * Behavior: Keeps all hosted-mode branching grounded in one explicit runtime helper.
 */
export function isHostedFrontendRuntime(): boolean {
  return resolveFrontendRuntimeMode() === "hosted";
}
