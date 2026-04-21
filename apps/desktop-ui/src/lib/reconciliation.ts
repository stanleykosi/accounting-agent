/*
Purpose: Provide API client helpers for the reconciliation review workspace.
Scope: Fetch reconciliations, items, trial balance, anomalies, dispositions, and approvals
       from same-origin API routes scoped to an entity and close run via the /api/entities proxy.
Dependencies: Native fetch, canonical reconciliation contract shapes.
*/

import {
  buildEntityCacheInvalidationPrefixes,
  invalidateClientCacheByPrefix,
  loadClientCachedValue,
} from "./client-cache";

/** Represent the filter state for the reconciliation review queue. */
export type ReconciliationReviewFilter =
  | "all"
  | "unresolved"
  | "matched"
  | "exception"
  | "unmatched";

/** Represent one reconciliation run summary returned by the API. */
export type ReconciliationSummary = {
  id: string;
  closeRunId: string;
  reconciliationType: string;
  status: string;
  summary: Record<string, unknown>;
  blockingReason: string | null;
  approvedByUserId: string | null;
  createdByUserId: string | null;
  itemCount: number;
  matchedCount: number;
  exceptionCount: number;
  createdAt: string;
  updatedAt: string;
};

/** Represent one reconciliation item (match result) returned by the API. */
export type ReconciliationItemSummary = {
  id: string;
  reconciliationId: string;
  sourceType: string;
  sourceRef: string;
  matchStatus: string;
  amount: string;
  differenceAmount: string;
  matchedTo: ReadonlyArray<{
    sourceType: string;
    sourceRef: string;
    amount: string | null;
    confidence: number | null;
  }>;
  explanation: string | null;
  requiresDisposition: boolean;
  disposition: string | null;
  dispositionReason: string | null;
  dispositionByUserId: string | null;
  dimensions: Record<string, unknown>;
  periodDate: string | null;
  createdAt: string;
  updatedAt: string;
};

/** Represent one trial balance account entry. */
export type TrialBalanceAccountEntry = {
  accountCode: string;
  accountName: string;
  accountType: string;
  debitBalance: string;
  creditBalance: string;
  netBalance: string;
  isActive: boolean;
};

/** Represent one reconciliation anomaly. */
export type ReconciliationAnomalySummary = {
  id: string;
  closeRunId: string;
  anomalyType: string;
  severity: string;
  accountCode: string | null;
  description: string;
  details: Record<string, unknown>;
  resolved: boolean;
  resolvedByUserId: string | null;
  createdAt: string;
};

/** Represent the reconciliation review workspace data. */
export type ReconciliationReviewWorkspaceData = {
  closeRunId: string;
  closeRunStatus: string;
  closeRunPeriodStart: string | null;
  closeRunPeriodEnd: string | null;
  closeRunActivePhase: string | null;
  closeRunBlockingReason: string | null;
  closeRunOperatingMode: string;
  closeRunOperatingModeDescription: string | null;
  bankReconciliationAvailable: boolean;
  trialBalanceReviewAvailable: boolean;
  reconciliations: ReadonlyArray<ReconciliationSummary>;
  items: ReadonlyArray<ReconciliationItemSummary>;
  anomalies: ReadonlyArray<ReconciliationAnomalySummary>;
  trialBalance: {
    snapshotNo: number;
    totalDebits: string;
    totalCredits: string;
    isBalanced: boolean;
    accountCount: number;
  } | null;
  queueCounts: {
    needsDecision: number;
    matched: number;
    exception: number;
    unmatched: number;
    actionableAnomalies: number;
    informationalAnomalies: number;
    pendingRunApprovals: number;
  };
};

/** Represent a disposition action value. */
export type DispositionActionValue =
  | "resolved"
  | "adjusted"
  | "accepted_as_is"
  | "escalated"
  | "pending_info";

export type ReconciliationRunResponse = {
  job_id: string | null;
  reconciliation_types: readonly string[];
  skipped_types: readonly string[];
  status: string;
  task_name: string;
  message: string | null;
};

/** Represent an API error from reconciliation endpoints. */
export class ReconciliationApiError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number | null = null,
    public readonly code: string | null = null,
  ) {
    super(message);
    this.name = "ReconciliationApiError";
  }
}

const RECONCILIATION_TYPE_LABELS: Readonly<Record<string, string>> = {
  bank_reconciliation: "Bank Reconciliation",
  ar_ageing: "AR Ageing",
  ap_ageing: "AP Ageing",
  intercompany: "Intercompany",
  payroll_control: "Payroll Control",
  fixed_assets: "Fixed Assets",
  loan_amortisation: "Loan Amortisation",
  accrual_tracker: "Accrual Tracker",
  budget_vs_actual: "Budget vs Actual",
  trial_balance: "Trial Balance",
};

const MATCH_STATUS_LABELS: Readonly<Record<string, string>> = {
  matched: "Matched",
  partially_matched: "Partially Matched",
  unmatched: "Unmatched",
  exception: "Exception",
};

const SEVERITY_COLORS: Readonly<Record<string, string>> = {
  blocking: "#b91c1c",
  warning: "#d97706",
  info: "#0284c7",
};

// ---------------------------------------------------------------------------
// API proxy path builder — mirrors the document review pattern
// ---------------------------------------------------------------------------

const ENTITIES_PROXY_BASE_PATH = "/api/entities";

/**
 * Purpose: Build the /api/entities proxy path for one entity and route segments.
 * Inputs: Entity UUID and an ordered array of route segments.
 * Outputs: A URL path string that routes through the Next.js /api/entities proxy
 *          to the FastAPI backend.
 * Behavior: Encodes each segment to prevent path injection.
 */
function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `${ENTITIES_PROXY_BASE_PATH}/${encodedSegments.join("/")}`;
}

// ---------------------------------------------------------------------------
// Response normalizers — convert snake_case API payloads to camelCase UI shapes
// ---------------------------------------------------------------------------

/** Safely coerce an unknown value to a string, defaulting when nullish. */
function asString(value: unknown, fallback = ""): string {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

/** Safely coerce an unknown value to a number, defaulting when invalid. */
function asNumber(value: unknown, fallback = 0): number {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? fallback : parsed;
  }
  return fallback;
}

/** Safely coerce an unknown value to a boolean. */
function asBool(value: unknown, fallback = false): boolean {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return value !== "" && value !== "false" && value !== "0";
  return fallback;
}

/**
 * Purpose: Normalize a snake_case reconciliation summary from the API into a camelCase UI shape.
 * Inputs: Raw API response object.
 * Outputs: A typed ReconciliationSummary with camelCase keys.
 */
function parseReconciliationSummary(raw: Record<string, unknown>): ReconciliationSummary {
  return {
    id: asString(raw.id),
    closeRunId: asString(raw.close_run_id),
    reconciliationType: asString(raw.reconciliation_type),
    status: asString(raw.status),
    summary: (raw.summary as Record<string, unknown>) ?? {},
    blockingReason: (raw.blocking_reason as string | null) ?? null,
    approvedByUserId: (raw.approved_by_user_id as string | null) ?? null,
    createdByUserId: (raw.created_by_user_id as string | null) ?? null,
    itemCount: asNumber(raw.item_count),
    matchedCount: asNumber(raw.matched_count),
    exceptionCount: asNumber(raw.exception_count),
    createdAt: asString(raw.created_at),
    updatedAt: asString(raw.updated_at),
  };
}

/**
 * Purpose: Normalize a snake_case reconciliation item from the API into a camelCase UI shape.
 * Inputs: Raw API response object.
 * Outputs: A typed ReconciliationItemSummary with camelCase keys.
 */
function parseReconciliationItem(raw: Record<string, unknown>): ReconciliationItemSummary {
  const matchedToRaw = raw.matched_to as Array<Record<string, unknown>> | undefined;
  const matchedTo = (matchedToRaw ?? []).map((cp) => ({
    sourceType: asString(cp.source_type),
    sourceRef: asString(cp.source_ref),
    amount: (cp.amount as string | null) ?? null,
    confidence: (cp.confidence as number | null) ?? null,
  }));

  return {
    id: asString(raw.id),
    reconciliationId: asString(raw.reconciliation_id),
    sourceType: asString(raw.source_type),
    sourceRef: asString(raw.source_ref),
    matchStatus: asString(raw.match_status),
    amount: asString(raw.amount, "0"),
    differenceAmount: asString(raw.difference_amount, "0"),
    matchedTo,
    explanation: (raw.explanation as string | null) ?? null,
    requiresDisposition: asBool(raw.requires_disposition),
    disposition: (raw.disposition as string | null) ?? null,
    dispositionReason: (raw.disposition_reason as string | null) ?? null,
    dispositionByUserId: (raw.disposition_by_user_id as string | null) ?? null,
    dimensions: (raw.dimensions as Record<string, unknown>) ?? {},
    periodDate: (raw.period_date as string | null) ?? null,
    createdAt: asString(raw.created_at),
    updatedAt: asString(raw.updated_at),
  };
}

/**
 * Purpose: Normalize a snake_case anomaly summary from the API into a camelCase UI shape.
 * Inputs: Raw API response object.
 * Outputs: A typed ReconciliationAnomalySummary with camelCase keys.
 */
function parseAnomalySummary(raw: Record<string, unknown>): ReconciliationAnomalySummary {
  return {
    id: asString(raw.id),
    closeRunId: asString(raw.close_run_id),
    anomalyType: asString(raw.anomaly_type),
    severity: asString(raw.severity),
    accountCode: (raw.account_code as string | null) ?? null,
    description: asString(raw.description),
    details: (raw.details as Record<string, unknown>) ?? {},
    resolved: asBool(raw.resolved),
    resolvedByUserId: (raw.resolved_by_user_id as string | null) ?? null,
    createdAt: asString(raw.created_at),
  };
}

// ---------------------------------------------------------------------------
// Workspace loader
// ---------------------------------------------------------------------------

/**
 * Purpose: Fetch the full reconciliation review workspace for one entity close run.
 * Inputs: Entity UUID and close-run UUID.
 * Outputs: A ReconciliationReviewWorkspaceData object containing reconciliations, items,
 *          anomalies, trial balance summary, and queue counts.
 * Behavior: Issues parallel API requests through the /api/entities proxy, normalizes
 *           snake_case responses to camelCase, and composes results into a workspace object.
 */
export async function readReconciliationReviewWorkspace(
  entityId: string,
  closeRunId: string,
): Promise<ReconciliationReviewWorkspaceData> {
  const [closeRunResp, reconciliationsResp, anomaliesResp, trialBalanceResp] = await Promise.allSettled([
    fetchWithAuth(buildEntityProxyPath(entityId, ["close-runs", closeRunId])),
    fetchWithAuth(buildEntityProxyPath(entityId, ["close-runs", closeRunId, "reconciliations"])),
    fetchWithAuth(buildEntityProxyPath(entityId, ["close-runs", closeRunId, "anomalies"])),
    fetchWithAuth(buildEntityProxyPath(entityId, ["close-runs", closeRunId, "trial-balance"])),
  ]);

  const closeRunRaw =
    closeRunResp.status === "fulfilled"
      ? (closeRunResp.value as {
          status?: string;
          period_start?: string | null;
          period_end?: string | null;
          operating_mode?: {
            mode?: string | null;
            description?: string | null;
            bank_reconciliation_available?: boolean | null;
            trial_balance_review_available?: boolean | null;
          } | null;
          workflow_state?: {
            active_phase?: string | null;
            phase_states?: Array<{
              phase?: string;
              status?: string;
              blocking_reason?: string | null;
            }>;
          } | null;
        })
      : null;
  const workflowState = closeRunRaw?.workflow_state ?? null;
  const operatingMode = closeRunRaw?.operating_mode ?? null;
  const activePhase = (workflowState?.active_phase as string | null) ?? null;
  const blockingReason =
    ((workflowState?.phase_states ?? []).find(
      (phaseState) =>
        asString(phaseState.phase) === activePhase && asString(phaseState.status) === "blocked",
    )?.blocking_reason ?? null);

  const reconciliationsRaw =
    reconciliationsResp.status === "fulfilled"
      ? (reconciliationsResp.value as { reconciliations?: Array<Record<string, unknown>> })
      : {};
  const reconciliations = (reconciliationsRaw.reconciliations ?? []).map(parseReconciliationSummary);

  const anomaliesRaw =
    anomaliesResp.status === "fulfilled"
      ? (anomaliesResp.value as { anomalies?: Array<Record<string, unknown>> })
      : {};
  const anomalies = (anomaliesRaw.anomalies ?? []).map(parseAnomalySummary);

  const trialBalanceRaw =
    trialBalanceResp.status === "fulfilled"
      ? (trialBalanceResp.value as {
          snapshot?: {
            snapshot_no?: number;
            total_debits?: string;
            total_credits?: string;
            is_balanced?: boolean;
            account_count?: number;
          };
        } | null)
      : null;

  const tbSnapshot = trialBalanceRaw?.snapshot;
  const trialBalance =
    tbSnapshot && (tbSnapshot.snapshot_no ?? 0) > 0
      ? {
          snapshotNo: tbSnapshot.snapshot_no ?? 0,
          totalDebits: tbSnapshot.total_debits ?? "0.00",
          totalCredits: tbSnapshot.total_credits ?? "0.00",
          isBalanced: tbSnapshot.is_balanced ?? true,
          accountCount: tbSnapshot.account_count ?? 0,
        }
      : null;

  // Fetch items for each reconciliation
  let allItems: ReconciliationItemSummary[] = [];
  for (const rec of reconciliations) {
    try {
      const itemsResp = (await fetchWithAuth(
        buildEntityProxyPath(entityId, [
          "close-runs",
          closeRunId,
          "reconciliations",
          rec.id,
          "items",
        ]),
      )) as { items?: Array<Record<string, unknown>> };
      const items = (itemsResp.items ?? []).map(parseReconciliationItem);
      allItems = allItems.concat(items);
    } catch {
      // Skip items for this reconciliation if the fetch fails
    }
  }

  const actionableAnomalyCount = anomalies.filter(
    (a) => !a.resolved && a.severity !== "info",
  ).length;
  const informationalAnomalyCount = anomalies.filter(
    (a) => !a.resolved && a.severity === "info",
  ).length;
  const pendingRunApprovals = reconciliations.filter((r) => r.status !== "approved").length;

  const queueCounts = {
    needsDecision: allItems.filter(
      (i) => i.requiresDisposition && i.disposition === null,
    ).length,
    matched: allItems.filter((i) => i.matchStatus === "matched").length,
    exception: allItems.filter(
      (i) => i.matchStatus === "exception" || i.matchStatus === "unmatched",
    ).length,
    unmatched: allItems.filter((i) => i.matchStatus === "unmatched").length,
    actionableAnomalies: actionableAnomalyCount,
    informationalAnomalies: informationalAnomalyCount,
    pendingRunApprovals,
  };

  return {
    closeRunId,
    closeRunStatus: asString(closeRunRaw?.status, "draft"),
    closeRunPeriodStart: (closeRunRaw?.period_start as string | null) ?? null,
    closeRunPeriodEnd: (closeRunRaw?.period_end as string | null) ?? null,
    closeRunActivePhase: activePhase,
    closeRunBlockingReason: blockingReason,
    closeRunOperatingMode: asString(operatingMode?.mode, "source_documents_only"),
    closeRunOperatingModeDescription: (operatingMode?.description as string | null) ?? null,
    bankReconciliationAvailable: asBool(operatingMode?.bank_reconciliation_available),
    trialBalanceReviewAvailable: asBool(operatingMode?.trial_balance_review_available),
    reconciliations,
    items: allItems,
    anomalies,
    trialBalance,
    queueCounts,
  };
}

// ---------------------------------------------------------------------------
// Mutation helpers
// ---------------------------------------------------------------------------

/**
 * Purpose: Record a reviewer disposition for one reconciliation item.
 * Inputs: Entity UUID, close-run UUID, item UUID, disposition action, and reasoning.
 * Outputs: The disposition result from the API.
 * Behavior: POSTs to the disposition endpoint and returns the parsed result.
 */
export async function submitDispositionItem(
  entityId: string,
  closeRunId: string,
  itemId: string,
  disposition: DispositionActionValue,
  reason: string,
): Promise<{ itemId: string; disposition: string; requiresFurtherAction: boolean }> {
  const result = await fetchWithAuth(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "items", itemId, "disposition"]),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disposition, reason }),
    },
  ) as Record<string, unknown>;
  return {
    itemId: asString(result.item_id, itemId),
    disposition: asString(result.disposition, disposition),
    requiresFurtherAction: asBool(result.requires_further_action),
  };
}

/**
 * Purpose: Record bulk dispositions for multiple reconciliation items.
 * Inputs: Entity UUID, close-run UUID, item UUIDs, disposition action, and reasoning.
 * Outputs: Count of disposed and failed items.
 * Behavior: POSTs to the bulk disposition endpoint.
 */
export async function submitBulkDisposition(
  entityId: string,
  closeRunId: string,
  itemIds: string[],
  disposition: DispositionActionValue,
  reason: string,
): Promise<{ disposedCount: number; failedCount: number; failedItemIds: string[] }> {
  const result = await fetchWithAuth(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "disposition", "bulk"]),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_ids: itemIds, disposition, reason }),
    },
  ) as Record<string, unknown>;
  return {
    disposedCount: asNumber(result.disposed_count),
    failedCount: asNumber(result.failed_count),
    failedItemIds: ((result.failed_item_ids as string[]) ?? []).map((id) => asString(id)),
  };
}

/**
 * Purpose: Approve a reconciliation run.
 * Inputs: Entity UUID, close-run UUID, reconciliation UUID, and approval reason.
 * Outputs: The approval result from the API.
 * Behavior: POSTs to the approve endpoint. Throws if pending dispositions block approval.
 */
export async function approveReconciliation(
  entityId: string,
  closeRunId: string,
  reconciliationId: string,
  reason: string,
): Promise<{ reconciliationId: string; status: string; approvedByUserId: string }> {
  const result = await fetchWithAuth(
    buildEntityProxyPath(entityId, [
      "close-runs",
      closeRunId,
      "reconciliations",
      reconciliationId,
      "approve",
    ]),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    },
  ) as Record<string, unknown>;
  return {
    reconciliationId: asString(result.reconciliation_id, reconciliationId),
    status: asString(result.status),
    approvedByUserId: asString(result.approved_by_user_id),
  };
}

/**
 * Purpose: Trigger reconciliation execution for one close run.
 * Inputs: Entity UUID, close-run UUID, and optional reconciliation type filters.
 * Outputs: Durable job metadata for the queued reconciliation execution.
 * Behavior: POSTs to the close-run reconciliation run endpoint.
 */
export async function runReconciliation(
  entityId: string,
  closeRunId: string,
  reconciliationTypes?: readonly string[],
): Promise<ReconciliationRunResponse> {
  const result = await fetchWithAuth(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "reconciliations", "run"]),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        reconciliation_types: reconciliationTypes && reconciliationTypes.length > 0
          ? reconciliationTypes
          : null,
      }),
    },
  ) as Record<string, unknown>;
  return {
    job_id: (() => {
      const value = result.job_id;
      return value === null || value === undefined ? null : asString(value);
    })(),
    reconciliation_types: ((result.reconciliation_types as string[]) ?? []).map((value) =>
      asString(value),
    ),
    skipped_types: ((result.skipped_types as string[]) ?? []).map((value) => asString(value)),
    status: asString(result.status),
    task_name: asString(result.task_name),
    message: (() => {
      const value = result.message;
      return typeof value === "string" ? value : null;
    })(),
  };
}

/**
 * Purpose: Resolve a reconciliation anomaly.
 * Inputs: Entity UUID, close-run UUID, anomaly UUID, and resolution note.
 * Outputs: The resolved anomaly summary.
 * Behavior: POSTs to the resolve endpoint.
 */
export async function resolveAnomaly(
  entityId: string,
  closeRunId: string,
  anomalyId: string,
  resolutionNote: string,
): Promise<ReconciliationAnomalySummary> {
  const result = await fetchWithAuth(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "anomalies", anomalyId, "resolve"]),
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolution_note: resolutionNote }),
    },
  ) as Record<string, unknown>;
  return parseAnomalySummary(result);
}

// ---------------------------------------------------------------------------
// Filtering and formatting
// ---------------------------------------------------------------------------

/**
 * Purpose: Filter reconciliation items by the active review tab.
 * Inputs: Array of items and the active filter value.
 * Outputs: A filtered array matching the selected filter semantics.
 * Behavior: Maps filter values to item status predicates.
 */
export function filterReconciliationItems(
  items: ReadonlyArray<ReconciliationItemSummary>,
  filter: ReconciliationReviewFilter,
): ReadonlyArray<ReconciliationItemSummary> {
  switch (filter) {
    case "unresolved":
      return items.filter((i) => i.requiresDisposition && i.disposition === null);
    case "matched":
      return items.filter((i) => i.matchStatus === "matched" || i.matchStatus === "partially_matched");
    case "exception":
      return items.filter((i) => i.matchStatus === "exception");
    case "unmatched":
      return items.filter((i) => i.matchStatus === "unmatched");
    default:
      return items;
  }
}

/**
 * Purpose: Format a reconciliation type enum into a human-readable label.
 * Inputs: A reconciliation type string (snake_case enum value).
 * Outputs: A display label string.
 * Behavior: Uses a lookup map; falls back to title-casing the raw value.
 */
export function formatReconciliationTypeLabel(type: string): string {
  return RECONCILIATION_TYPE_LABELS[type] ?? type.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Purpose: Format a match status enum into a human-readable label.
 * Inputs: A match status string.
 * Outputs: A display label string.
 * Behavior: Uses a lookup map; falls back to title-casing the raw value.
 */
export function formatMatchStatusLabel(status: string): string {
  return MATCH_STATUS_LABELS[status] ?? status.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Purpose: Return a CSS color for an anomaly severity level.
 * Inputs: A severity string (blocking, warning, info).
 * Outputs: A hex color string.
 * Behavior: Uses a severity-color map; falls back to gray.
 */
export function getSeverityColor(severity: string): string {
  return SEVERITY_COLORS[severity] ?? "#6b7280";
}

// ---------------------------------------------------------------------------
// Internal fetch wrapper
// ---------------------------------------------------------------------------

/**
 * Purpose: Fetch JSON from a same-origin API route and parse the response.
 * Inputs: A URL string and optional RequestInit options.
 * Outputs: The parsed JSON response body.
 * Behavior: Throws ReconciliationApiError on non-2xx responses.
 */
async function fetchWithAuth(url: string, init?: RequestInit): Promise<unknown> {
  const requestMethod = normalizeRequestMethod(init?.method);

  const performRequest = async (): Promise<unknown> => {
    const response = await fetch(url, {
      ...init,
      cache: "no-store",
      credentials: "include",
    });

    if (!response.ok) {
      let message = `API request failed: ${response.status} ${response.statusText}`;
      let code: string | null = null;
      try {
        const body = (await response.json()) as Record<string, unknown>;
        if (typeof body.detail === "object" && body.detail !== null) {
          const detail = body.detail as Record<string, unknown>;
          if (typeof detail.message === "string") {
            message = detail.message;
          }
          if (typeof detail.code === "string") {
            code = detail.code;
          }
        } else if (typeof body.detail === "string") {
          message = body.detail;
        }
      } catch {
        // Response body is not JSON; use default message
      }
      throw new ReconciliationApiError(message, response.status, code);
    }

    return response.json();
  };

  if (requestMethod === "GET") {
    return loadClientCachedValue(url, performRequest);
  }

  const payload = await performRequest();
  invalidateClientCacheByPrefix(buildEntityCacheInvalidationPrefixes(url));
  return payload;
}

function normalizeRequestMethod(value: string | null | undefined): string {
  return (value ?? "GET").toUpperCase();
}
