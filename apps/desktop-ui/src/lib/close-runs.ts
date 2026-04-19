/*
Purpose: Centralize same-origin close-run reads and derived desktop dashboard helpers.
Scope: Entity-scoped close-run list/detail requests, runtime response validation, and UI-friendly status derivation.
Dependencies: Shared UI domain metadata, entity workspace helpers, and browser Fetch APIs routed through the existing entity proxy.
*/

import {
  closeRunPhaseStatusDefinitions,
  closeRunStatusDefinitions,
  getWorkflowPhaseDefinition,
  type CloseRunPhaseStatus,
  type CloseRunStatus,
  type PhaseProgressItem,
  type WorkflowPhase,
} from "@accounting-ai-agent/ui";
import { readEntityWorkspace, type EntityWorkspace } from "./entities/api";

export type CloseRunPhaseStateSummary = {
  blockingReason: string | null;
  completedAt: string | null;
  phase: WorkflowPhase;
  status: CloseRunPhaseStatus;
};

export type CloseRunWorkflowStateSummary = {
  activePhase: WorkflowPhase | null;
  phaseStates: readonly CloseRunPhaseStateSummary[];
  status: CloseRunStatus;
};

export type CloseRunOperatingMode =
  | "source_documents_only"
  | "working_ledger"
  | "imported_general_ledger"
  | "trial_balance_only";

export type CloseRunOperatingModeSummary = {
  mode: CloseRunOperatingMode;
  description: string;
  hasGeneralLedgerBaseline: boolean;
  hasTrialBalanceBaseline: boolean;
  hasWorkingLedgerEntries: boolean;
  bankReconciliationAvailable: boolean;
  trialBalanceReviewAvailable: boolean;
  journalPostingAvailable: boolean;
  generalLedgerExportAvailable: boolean;
};

export type CloseRunSummary = {
  approvedAt: string | null;
  approvedByUserId: string | null;
  archivedAt: string | null;
  createdAt: string;
  currentVersionNo: number;
  entityId: string;
  id: string;
  openedByUserId: string;
  operatingMode: CloseRunOperatingModeSummary;
  periodEnd: string;
  periodStart: string;
  reopenedFromCloseRunId: string | null;
  reportingCurrency: string;
  status: CloseRunStatus;
  updatedAt: string;
  workflowState: CloseRunWorkflowStateSummary;
};

export type CloseRunWorkspaceData = {
  closeRun: CloseRunSummary;
  closeRuns: readonly CloseRunSummary[];
  entity: EntityWorkspace;
};

export type CloseRunDeleteResponse = {
  canceled_job_count: number;
  deleted_close_run_id: string;
  deleted_document_count: number;
  deleted_journal_count: number;
  deleted_recommendation_count: number;
  deleted_report_run_count: number;
  deleted_thread_count: number;
};

export type CloseRunAttentionTone = "default" | "success" | "warning";

export type CloseRunAttention = {
  detail: string;
  label: string;
  tone: CloseRunAttentionTone;
};

export type CreateCloseRunRequest = {
  allow_duplicate_period?: boolean;
  duplicate_period_reason?: string;
  period_end: string;
  period_start: string;
  reporting_currency?: string | null;
};

export type TransitionCloseRunRequest = {
  reason?: string | null;
  target_phase: WorkflowPhase;
};

export type CloseRunApiErrorCode =
  | "approval_blocked"
  | "archive_not_allowed"
  | "close_run_not_found"
  | "delete_not_allowed"
  | "duplicate_period"
  | "entity_archived"
  | "entity_not_found"
  | "integrity_conflict"
  | "invalid_transition"
  | "phase_blocked"
  | "reopen_not_allowed"
  | "session_expired"
  | "session_required"
  | "unknown_error"
  | "user_disabled"
  | "validation_error";

/**
 * Purpose: Represent a structured close-run API failure that UI callers can inspect safely.
 * Inputs: Stable error code, HTTP status code, and an operator-facing message emitted by the API boundary.
 * Outputs: A typed Error instance that preserves both human and machine-readable diagnostics.
 * Behavior: Keeps fail-fast close-run messages available to dashboard and detail surfaces.
 */
export class CloseRunApiError extends Error {
  readonly code: CloseRunApiErrorCode;
  readonly statusCode: number;

  constructor(
    options: Readonly<{ code: CloseRunApiErrorCode; message: string; statusCode: number }>,
  ) {
    super(options.message);
    this.name = "CloseRunApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

const ENTITIES_PROXY_BASE_PATH = "/api/entities";

/**
 * Purpose: Read all close runs for one entity workspace through the same-origin proxy.
 * Inputs: The owning entity UUID.
 * Outputs: Normalized close-run summaries ordered by period and version.
 * Behavior: Leaves the FastAPI close-run service as the source of truth while converting payloads into UI-friendly shapes.
 */
export async function listCloseRuns(entityId: string): Promise<readonly CloseRunSummary[]> {
  const payload = await closeRunRequest<unknown>(buildEntityProxyPath(entityId, ["close-runs"]), {
    method: "GET",
  });
  return parseCloseRunListResponse(payload);
}

/**
 * Purpose: Create a close run for one entity from the hosted UI.
 * Inputs: Entity UUID and period/create payload.
 * Outputs: The created close-run summary.
 * Behavior: Uses the same entity proxy path so session handling stays same-origin.
 */
export async function createCloseRun(
  entityId: string,
  payload: CreateCloseRunRequest,
): Promise<CloseRunSummary> {
  const response = await closeRunRequest<unknown>(buildEntityProxyPath(entityId, ["close-runs"]), {
    method: "POST",
    body: JSON.stringify(payload),
  });
  return parseCloseRunSummary(response);
}

/**
 * Purpose: Read one close run in detail for dashboard or overview rendering.
 * Inputs: Entity UUID and close-run UUID.
 * Outputs: One normalized close-run summary.
 * Behavior: Uses the same nested entity proxy path as the document and reconciliation surfaces.
 */
export async function readCloseRun(entityId: string, closeRunId: string): Promise<CloseRunSummary> {
  const payload = await closeRunRequest<unknown>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId]),
    {
      method: "GET",
    },
  );
  return parseCloseRunSummary(payload);
}

export async function transitionCloseRun(
  entityId: string,
  closeRunId: string,
  payload: TransitionCloseRunRequest,
): Promise<CloseRunSummary> {
  const response = await closeRunRequest<unknown>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "transition"]),
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
  if (!isRecord(response) || !isRecord(response.close_run)) {
    throw new Error("Invalid close-run transition response payload.");
  }
  return parseCloseRunSummary(response.close_run);
}

export async function approveCloseRun(
  entityId: string,
  closeRunId: string,
  reason?: string | null,
): Promise<CloseRunSummary> {
  const response = await closeRunRequest<unknown>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "approve"]),
    {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  return parseCloseRunSummary(response);
}

export async function archiveCloseRun(
  entityId: string,
  closeRunId: string,
  reason?: string | null,
): Promise<CloseRunSummary> {
  const response = await closeRunRequest<unknown>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "archive"]),
    {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
  return parseCloseRunSummary(response);
}

export async function deleteCloseRun(
  entityId: string,
  closeRunId: string,
): Promise<CloseRunDeleteResponse> {
  return closeRunRequest<CloseRunDeleteResponse>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId]),
    {
      method: "DELETE",
    },
  );
}

/**
 * Purpose: Load the full desktop close-run workspace context used by the overview page.
 * Inputs: Entity UUID and close-run UUID from the active route.
 * Outputs: Entity workspace detail, the selected close run, and sibling close runs for the entity.
 * Behavior: Fetches the three required resources in parallel so the overview can render one coherent workspace snapshot.
 */
export async function readCloseRunWorkspace(
  entityId: string,
  closeRunId: string,
): Promise<CloseRunWorkspaceData> {
  const [entity, closeRun, closeRuns] = await Promise.all([
    readEntityWorkspace(entityId),
    readCloseRun(entityId, closeRunId),
    listCloseRuns(entityId),
  ]);
  return {
    closeRun,
    closeRuns,
    entity,
  };
}

/**
 * Purpose: Format the period range of a close run into a compact human-readable label.
 * Inputs: The normalized close-run summary.
 * Outputs: A short date-range string suitable for headers and queue rows.
 * Behavior: Falls back to raw ISO strings when the date values cannot be parsed.
 */
export function formatCloseRunPeriod(closeRun: Readonly<CloseRunSummary>): string {
  const start = safeParseDate(closeRun.periodStart);
  const end = safeParseDate(closeRun.periodEnd);
  if (start === null || end === null) {
    return `${closeRun.periodStart} to ${closeRun.periodEnd}`;
  }

  return `${start.toLocaleDateString("en-NG", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  })} to ${end.toLocaleDateString("en-NG", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  })}`;
}

/**
 * Purpose: Format a close-run timestamp into one operator-facing line.
 * Inputs: An ISO timestamp or null.
 * Outputs: A formatted local timestamp or an em dash placeholder.
 * Behavior: Uses the Nigerian locale to match the rest of the desktop workspace.
 */
export function formatCloseRunDateTime(value: string | null): string {
  if (value === null) {
    return "Not recorded";
  }

  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

/**
 * Purpose: Build the shared phase-progress items expected by the desktop UI component.
 * Inputs: One normalized close-run summary.
 * Outputs: Ordered phase items with current-phase emphasis and blocker context.
 * Behavior: Prefers the workflow state's active phase and falls back to the first ready or in-progress phase when needed.
 */
export function buildPhaseProgressItems(
  closeRun: Readonly<CloseRunSummary>,
): readonly PhaseProgressItem[] {
  const activePhase = findActivePhase(closeRun);

  return closeRun.workflowState.phaseStates.map((phaseState) => ({
    blockingReason: phaseState.blockingReason,
    completedAt: phaseState.completedAt,
    isCurrent: activePhase?.phase === phaseState.phase,
    phase: phaseState.phase,
    status: phaseState.status,
  }));
}

/**
 * Purpose: Resolve the workflow phase that currently needs operator attention.
 * Inputs: One normalized close-run summary.
 * Outputs: The active phase record, or null when the run is fully signed off without further phase work.
 * Behavior: Uses the workflow state's declared active phase first because the backend owns gate sequencing.
 */
export function findActivePhase(
  closeRun: Readonly<CloseRunSummary>,
): CloseRunPhaseStateSummary | null {
  const activePhaseCode = closeRun.workflowState.activePhase;
  if (activePhaseCode !== null) {
    const activePhase = closeRun.workflowState.phaseStates.find(
      (phaseState) => phaseState.phase === activePhaseCode,
    );
    if (activePhase) {
      return activePhase;
    }
  }

  return (
    closeRun.workflowState.phaseStates.find(
      (phaseState) => phaseState.status === "blocked" || phaseState.status === "ready",
    ) ??
    closeRun.workflowState.phaseStates.find((phaseState) => phaseState.status === "in_progress") ??
    null
  );
}

/**
 * Purpose: Resolve the first blocking phase for review-queue prioritization.
 * Inputs: One normalized close-run summary.
 * Outputs: The blocking phase record or null when no gate is blocked.
 * Behavior: Preserves canonical phase order by scanning the already ordered phase state list.
 */
export function findBlockingPhase(
  closeRun: Readonly<CloseRunSummary>,
): CloseRunPhaseStateSummary | null {
  return (
    closeRun.workflowState.phaseStates.find((phaseState) => phaseState.status === "blocked") ?? null
  );
}

/**
 * Purpose: Derive a short dashboard-ready attention summary for one close run.
 * Inputs: One normalized close-run summary.
 * Outputs: A tone, label, and detail string suitable for queues and hero cards.
 * Behavior: Prioritizes blockers first, then lifecycle release states, then the currently active phase.
 */
export function deriveCloseRunAttention(closeRun: Readonly<CloseRunSummary>): CloseRunAttention {
  const blockingPhase = findBlockingPhase(closeRun);
  if (blockingPhase !== null) {
    const phase = getWorkflowPhaseDefinition(blockingPhase.phase);
    return {
      detail:
        blockingPhase.blockingReason ?? `${phase.label} is blocked and needs reviewer action.`,
      label: `${phase.label} blocked`,
      tone: "warning",
    };
  }

  if (
    closeRun.status === "approved" ||
    closeRun.status === "exported" ||
    closeRun.status === "archived"
  ) {
    return {
      detail: `${getCloseRunStatusLabel(closeRun.status)} at ${formatCloseRunDateTime(
        closeRun.archivedAt ?? closeRun.approvedAt ?? closeRun.updatedAt,
      )}.`,
      label: `${getCloseRunStatusLabel(closeRun.status)} close run`,
      tone: "success",
    };
  }

  const activePhase = findActivePhase(closeRun);
  if (activePhase !== null) {
    const phase = getWorkflowPhaseDefinition(activePhase.phase);
    return {
      detail:
        activePhase.status === "ready"
          ? `${phase.label} passed its gate checks and can advance.`
          : `${phase.label} is the current working phase for this period.`,
      label: `${phase.label} in focus`,
      tone: activePhase.status === "ready" ? "success" : "default",
    };
  }

  return {
    detail: "This close run has not begun downstream workflow work yet.",
    label: "Awaiting first phase action",
    tone: "default",
  };
}

/**
 * Purpose: Resolve the operator-facing label for a close-run lifecycle status.
 * Inputs: One canonical close-run status code.
 * Outputs: The shared label exposed by the UI domain catalog.
 * Behavior: Fails fast when the status metadata has drifted from the code vocabulary.
 */
export function getCloseRunStatusLabel(status: CloseRunStatus): string {
  const definition = closeRunStatusDefinitions.find((item) => item.code === status);
  if (!definition) {
    throw new Error(`Unsupported close-run status: ${status}`);
  }
  return definition.label;
}

/**
 * Purpose: Resolve the operator-facing label for a close-run phase status.
 * Inputs: One canonical phase-status code.
 * Outputs: The shared label exposed by the UI domain catalog.
 * Behavior: Fails fast when the shared UI metadata has drifted from the API vocabulary.
 */
export function getCloseRunPhaseStatusLabel(status: CloseRunPhaseStatus): string {
  const definition = closeRunPhaseStatusDefinitions.find((item) => item.code === status);
  if (!definition) {
    throw new Error(`Unsupported close-run phase status: ${status}`);
  }
  return definition.label;
}

async function closeRunRequest<TResponse>(
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
    throw buildCloseRunApiError(response.status, payload);
  }

  return payload as TResponse;
}

function parseCloseRunListResponse(payload: unknown): readonly CloseRunSummary[] {
  if (!isRecord(payload) || !Array.isArray(payload.close_runs)) {
    throw new Error("Invalid close-run list response payload.");
  }

  return payload.close_runs.map((closeRun) => parseCloseRunSummary(closeRun));
}

function parseCloseRunSummary(payload: unknown): CloseRunSummary {
  if (!isRecord(payload)) {
    throw new Error("Invalid close-run summary payload.");
  }

  const workflowStatePayload = payload.workflow_state;
  if (!isRecord(workflowStatePayload) || !Array.isArray(workflowStatePayload.phase_states)) {
    throw new Error("Invalid close-run workflow state payload.");
  }
  const operatingModePayload = payload.operating_mode;
  if (!isRecord(operatingModePayload)) {
    throw new Error("Invalid close-run operating-mode payload.");
  }

  return {
    approvedAt: asOptionalString(payload.approved_at),
    approvedByUserId: asOptionalString(payload.approved_by_user_id),
    archivedAt: asOptionalString(payload.archived_at),
    createdAt: asString(payload.created_at),
    currentVersionNo: asNumber(payload.current_version_no),
    entityId: asString(payload.entity_id),
    id: asString(payload.id),
    openedByUserId: asString(payload.opened_by_user_id),
    operatingMode: parseOperatingModeSummary(operatingModePayload),
    periodEnd: asString(payload.period_end),
    periodStart: asString(payload.period_start),
    reopenedFromCloseRunId: asOptionalString(payload.reopened_from_close_run_id),
    reportingCurrency: asString(payload.reporting_currency),
    status: asCloseRunStatus(payload.status),
    updatedAt: asString(payload.updated_at),
    workflowState: {
      activePhase: asOptionalWorkflowPhase(workflowStatePayload.active_phase),
      phaseStates: workflowStatePayload.phase_states.map((phaseState) =>
        parsePhaseStateSummary(phaseState),
      ),
      status: asCloseRunStatus(workflowStatePayload.status),
    },
  };
}

function parseOperatingModeSummary(payload: Record<string, unknown>): CloseRunOperatingModeSummary {
  return {
    mode: asCloseRunOperatingMode(payload.mode),
    description: asString(payload.description),
    hasGeneralLedgerBaseline: asBoolean(payload.has_general_ledger_baseline),
    hasTrialBalanceBaseline: asBoolean(payload.has_trial_balance_baseline),
    hasWorkingLedgerEntries: asBoolean(payload.has_working_ledger_entries),
    bankReconciliationAvailable: asBoolean(payload.bank_reconciliation_available),
    trialBalanceReviewAvailable: asBoolean(payload.trial_balance_review_available),
    journalPostingAvailable: asBoolean(payload.journal_posting_available, true),
    generalLedgerExportAvailable: asBoolean(payload.general_ledger_export_available),
  };
}

function parsePhaseStateSummary(payload: unknown): CloseRunPhaseStateSummary {
  if (!isRecord(payload)) {
    throw new Error("Invalid close-run phase-state payload.");
  }

  return {
    blockingReason: asOptionalString(payload.blocking_reason),
    completedAt: asOptionalString(payload.completed_at),
    phase: asWorkflowPhase(payload.phase),
    status: asCloseRunPhaseStatus(payload.status),
  };
}

function buildCloseRunApiError(statusCode: number, payload: unknown): CloseRunApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      return new CloseRunApiError({
        code: asCloseRunApiErrorCode(detail.code),
        message:
          typeof detail.message === "string"
            ? detail.message
            : "The close-run request could not be completed.",
        statusCode,
      });
    }

    if (Array.isArray(detail)) {
      return new CloseRunApiError({
        code: "validation_error",
        message: "Review the close-run fields and try again.",
        statusCode,
      });
    }
  }

  return new CloseRunApiError({
    code: "unknown_error",
    message: "The close-run request failed. Reload the workspace and try again.",
    statusCode,
  });
}

function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `${ENTITIES_PROXY_BASE_PATH}/${encodedSegments.join("/")}`;
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

function asCloseRunApiErrorCode(value: unknown): CloseRunApiErrorCode {
  switch (value) {
    case "approval_blocked":
    case "archive_not_allowed":
    case "close_run_not_found":
    case "delete_not_allowed":
    case "duplicate_period":
    case "entity_archived":
    case "entity_not_found":
    case "integrity_conflict":
    case "invalid_transition":
    case "phase_blocked":
    case "reopen_not_allowed":
    case "session_expired":
    case "session_required":
    case "user_disabled":
      return value;
    default:
      return "unknown_error";
  }
}

function asCloseRunOperatingMode(value: unknown): CloseRunOperatingMode {
  switch (value) {
    case "source_documents_only":
    case "working_ledger":
    case "imported_general_ledger":
    case "trial_balance_only":
      return value;
    default:
      return "source_documents_only";
  }
}

function asString(value: unknown): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("Expected a non-empty string in close-run payload.");
  }

  return value;
}

function asOptionalString(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }

  return asString(value);
}

function asNumber(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error("Expected a finite number in close-run payload.");
  }

  return value;
}

function asBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") {
    return value;
  }
  if (value === null || value === undefined) {
    return fallback;
  }
  throw new Error("Expected a boolean in close-run payload.");
}

function asCloseRunStatus(value: unknown): CloseRunStatus {
  switch (value) {
    case "draft":
    case "in_review":
    case "approved":
    case "exported":
    case "archived":
    case "reopened":
      return value;
    default:
      throw new Error(`Unsupported close-run status value: ${String(value)}`);
  }
}

function asCloseRunPhaseStatus(value: unknown): CloseRunPhaseStatus {
  switch (value) {
    case "not_started":
    case "in_progress":
    case "blocked":
    case "ready":
    case "completed":
      return value;
    default:
      throw new Error(`Unsupported close-run phase status value: ${String(value)}`);
  }
}

function asWorkflowPhase(value: unknown): WorkflowPhase {
  switch (value) {
    case "collection":
    case "processing":
    case "reconciliation":
    case "reporting":
    case "review_signoff":
      return value;
    default:
      throw new Error(`Unsupported workflow phase value: ${String(value)}`);
  }
}

function asOptionalWorkflowPhase(value: unknown): WorkflowPhase | null {
  if (value === null || value === undefined) {
    return null;
  }

  return asWorkflowPhase(value);
}

function safeParseDate(value: string): Date | null {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? null : parsed;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
