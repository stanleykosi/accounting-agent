/*
Purpose: Centralize the portfolio bootstrap read used by the portfolio command center.
Scope: One aggregated dashboard request plus cache-backed snapshots for refresh-friendly hydration.
Dependencies: Entity and close-run UI types, browser cache helpers, and the same-origin dashboard API route.
*/

import type { CloseRunSummary } from "./close-runs";
import {
  buildEntityCacheInvalidationPrefixes,
  invalidateClientCacheByPrefix,
  loadClientCachedValue,
  readClientCacheSnapshot,
  writeClientCacheValue,
} from "./client-cache";
import type { EntitySummary } from "./entities/api";

export type DashboardEntityRuns = Readonly<{
  closeRuns: readonly CloseRunSummary[];
  entity: EntitySummary;
}>;

type DashboardBootstrapPayload = Readonly<{
  entries: readonly DashboardEntityRuns[];
}>;

const DASHBOARD_BOOTSTRAP_PATH = "/api/dashboard/bootstrap";
const DASHBOARD_CACHE_TTL_MS = 30_000;
const ENTITY_LIST_CACHE_PATH = "/api/entities";

/**
 * Purpose: Load the portfolio bootstrap in one same-origin request.
 * Inputs: None.
 * Outputs: Entity rows paired with their close-run summaries.
 * Behavior: Uses the shared browser cache so refreshes and route returns stay fast.
 */
export async function readDashboardBootstrap(): Promise<readonly DashboardEntityRuns[]> {
  const payload = await requestDashboardBootstrap();
  return payload.entries;
}

/**
 * Purpose: Read the latest fresh dashboard snapshot without issuing a network request.
 * Inputs: None.
 * Outputs: Cached portfolio entries or null when no fresh snapshot exists.
 * Behavior: Used by the portfolio page to render immediately after refresh.
 */
export function readDashboardBootstrapSnapshot(): readonly DashboardEntityRuns[] | null {
  const payload = readClientCacheSnapshot<DashboardBootstrapPayload>(DASHBOARD_BOOTSTRAP_PATH);
  return payload?.entries ?? null;
}

/**
 * Purpose: Clear the cached portfolio bootstrap after broad entity or close-run mutations.
 * Inputs: None.
 * Outputs: None.
 * Behavior: Keeps the dashboard command center aligned with the authoritative current state.
 */
export function invalidateDashboardBootstrap(): void {
  invalidateClientCacheByPrefix(buildEntityCacheInvalidationPrefixes(DASHBOARD_BOOTSTRAP_PATH));
}

async function requestDashboardBootstrap(): Promise<DashboardBootstrapPayload> {
  return loadClientCachedValue(
    DASHBOARD_BOOTSTRAP_PATH,
    async () => {
      const response = await fetch(DASHBOARD_BOOTSTRAP_PATH, {
        cache: "no-store",
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
        },
      });

      const payload = (await response.json()) as DashboardBootstrapPayload | { detail?: unknown };
      if (!response.ok) {
        throw new Error(resolveDashboardBootstrapErrorMessage(payload, response.status));
      }

      const normalizedPayload = payload as DashboardBootstrapPayload;
      primeDashboardDerivedCaches(normalizedPayload);
      return normalizedPayload;
    },
    DASHBOARD_CACHE_TTL_MS,
  );
}

function primeDashboardDerivedCaches(payload: Readonly<DashboardBootstrapPayload>): void {
  writeClientCacheValue(
    ENTITY_LIST_CACHE_PATH,
    {
      entities: payload.entries.map((entry) => entry.entity),
    },
    DASHBOARD_CACHE_TTL_MS,
  );

  payload.entries.forEach((entry) => {
    writeClientCacheValue(
      `/api/entities/${encodeURIComponent(entry.entity.id)}/close-runs`,
      {
        close_runs: entry.closeRuns,
      },
      DASHBOARD_CACHE_TTL_MS,
    );
  });
}

function resolveDashboardBootstrapErrorMessage(
  payload: DashboardBootstrapPayload | { detail?: unknown },
  statusCode: number,
): string {
  const detail = "detail" in payload ? payload.detail : null;
  if (typeof detail === "string") {
    return detail;
  }

  if (isRecord(detail) && typeof detail.message === "string") {
    return detail.message;
  }

  return `Dashboard bootstrap failed with status ${statusCode}.`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
