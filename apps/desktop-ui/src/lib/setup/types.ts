/*
Purpose: Define the shared local-runtime health contracts used by the desktop setup route and first-run UI.
Scope: Typed service-health summaries, overall readiness snapshots, and operator recovery command lists.
Dependencies: Shared by server-only health helpers, Next.js API routes, and the setup client component.
*/

import type { FrontendRuntimeMode } from "../runtime";

export type LocalServiceHealthStatus = "healthy" | "unhealthy";

export type LocalServiceHealthCheck = Readonly<{
  detail: string;
  endpoint: string | null;
  id: "api" | "minio" | "postgres" | "redis";
  label: string;
  latencyMs: number | null;
  status: LocalServiceHealthStatus;
}>;

export type DesktopSetupHealthSnapshot = Readonly<{
  checkedAt: string;
  mode: FrontendRuntimeMode;
  ready: boolean;
  recoveryCommands: readonly string[];
  services: readonly LocalServiceHealthCheck[];
}>;
