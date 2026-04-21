/*
Purpose: Render the close mission control page for one governed close run.
Scope: Live close-run workspace loading, lifecycle mutations, and direct routing into the core work surfaces.
Dependencies: React hooks, close-run/entity API helpers, shared workflow metadata, and the Quartz workspace components.
*/

"use client";

import { getWorkflowPhaseDefinition, type WorkflowPhase } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../../../../components/layout/QuartzIcons";
import {
  approveCloseRun,
  archiveCloseRun,
  CloseRunApiError,
  deleteCloseRun,
  deriveCloseRunAttention,
  findActivePhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunPhaseStatusLabel,
  getCloseRunStatusLabel,
  readCloseRunWorkspaceSnapshot,
  readCloseRunWorkspace,
  transitionCloseRun,
  type CloseRunSummary,
  type CloseRunWorkspaceData,
} from "../../../../../../lib/close-runs";
import { EntityApiError } from "../../../../../../lib/entities/api";
import { requireRouteParam } from "../../../../../../lib/route-params";

type MissionTile = {
  label: string;
  meta: string;
  tone?: "error" | "success";
  value: string;
};

type MissionWorkstreamRow = {
  detail: string;
  href: string;
  isBlocked: boolean;
  isCurrent: boolean;
  phase: WorkflowPhase;
  phaseStatusLabel: string;
  statusTone: "error" | "neutral" | "success" | "warning";
  title: string;
};

type TimelineRow = {
  id: string;
  label: string;
  meta: string;
};

const workflowPhaseOrder: readonly WorkflowPhase[] = [
  "collection",
  "processing",
  "reconciliation",
  "reporting",
  "review_signoff",
];

export default function CloseRunOverviewPage(): ReactElement {
  const routeParams = useParams<{ closeRunId: string; entityId: string }>();
  const closeRunId = requireRouteParam(routeParams.closeRunId, "closeRunId");
  const entityId = requireRouteParam(routeParams.entityId, "entityId");
  const workspaceSnapshot = readCloseRunWorkspaceSnapshot(entityId, closeRunId);
  const router = useRouter();
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(() => workspaceSnapshot === null);
  const [isMutating, setIsMutating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<CloseRunWorkspaceData | null>(workspaceSnapshot);

  useEffect(() => {
    void loadCloseRunWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: setWorkspaceData,
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading close mission control...</div>
        </section>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The requested close run could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  const closeRun = workspaceData.closeRun;
  const attention = deriveCloseRunAttention(closeRun);
  const nextPhase = resolveNextWorkflowPhase(closeRun);
  const workstreamRows = buildMissionWorkstreamRows(closeRun, entityId);
  const missionTiles = buildMissionTiles(closeRun);
  const phaseRows = buildPhaseProgressRows(closeRun);
  const timelineRows = buildTimelineRows(closeRun);

  async function refreshWorkspace(): Promise<void> {
    await loadCloseRunWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: setWorkspaceData,
      onLoadingChange: setIsLoading,
    });
  }

  async function handleAdvanceCloseRun(): Promise<void> {
    if (nextPhase === null) {
      return;
    }

    setIsMutating(true);
    try {
      await transitionCloseRun(entityId, closeRun.id, {
        reason: "Advanced from close mission control",
        target_phase: nextPhase,
      });
      setStatusMessage(`Close run advanced into ${getWorkflowPhaseDefinition(nextPhase).label}.`);
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveCloseRunOverviewErrorMessage(error));
    } finally {
      setIsMutating(false);
    }
  }

  async function handleApproveCloseRun(): Promise<void> {
    setIsMutating(true);
    try {
      await approveCloseRun(entityId, closeRun.id, "Approved from close mission control");
      setStatusMessage("Close run approved.");
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveCloseRunOverviewErrorMessage(error));
    } finally {
      setIsMutating(false);
    }
  }

  async function handleArchiveExistingCloseRun(): Promise<void> {
    setIsMutating(true);
    try {
      await archiveCloseRun(entityId, closeRun.id, "Archived from close mission control");
      setStatusMessage("Close run archived.");
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveCloseRunOverviewErrorMessage(error));
    } finally {
      setIsMutating(false);
    }
  }

  async function handleDeleteCloseRun(): Promise<void> {
    const confirmed = window.confirm(
      "Delete this close run? This removes uploaded documents, recommendations, journals, reports, and close-run chat threads for the period.",
    );
    if (!confirmed) {
      return;
    }

    setIsMutating(true);
    try {
      await deleteCloseRun(entityId, closeRun.id);
      router.push(`/entities/${closeRun.entityId}`);
      router.refresh();
    } catch (error: unknown) {
      setErrorMessage(resolveCloseRunOverviewErrorMessage(error));
      setIsMutating(false);
    }
  }

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Close Mission Control</h1>
            <p className="quartz-page-subtitle">
              {workspaceData.entity.name} • {formatCloseRunPeriod(closeRun)}
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/chat`}
            >
              <QuartzIcon className="quartz-inline-icon" name="assistant" />
              Open Assistant
            </Link>
            <span className={`quartz-status-badge ${mapAttentionToneToBadge(attention.tone)}`}>
              {attention.label}
            </span>
          </div>
        </header>

        {statusMessage ? (
          <div className="status-banner success quartz-section" role="status">
            {statusMessage}
          </div>
        ) : null}

        {errorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid quartz-kpi-grid-triple">
            {missionTiles.map((tile, index) => (
              <article
                className={
                  index === missionTiles.length - 1
                    ? "quartz-kpi-tile highlight"
                    : "quartz-kpi-tile"
                }
                key={tile.label}
              >
                <p className="quartz-kpi-label">{tile.label}</p>
                <p className={`quartz-kpi-value ${tile.tone ?? ""}`.trim()}>{tile.value}</p>
                <p className="quartz-kpi-meta">{tile.meta}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Workflow Control Table</h2>
            <span className="quartz-queue-meta">
              {workstreamRows.length} governed workstream{workstreamRows.length === 1 ? "" : "s"}
            </span>
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Workflow Area</th>
                  <th>Current Gate</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {workstreamRows.map((row) => (
                  <tr
                    className={row.isBlocked ? "quartz-table-row error" : undefined}
                    key={row.phase}
                  >
                    <td>
                      <div className="quartz-table-primary">{row.title}</div>
                      <div className="quartz-table-secondary">
                        {getWorkflowPhaseDefinition(row.phase).description}
                      </div>
                    </td>
                    <td>
                      <div className="quartz-table-primary">{row.phaseStatusLabel}</div>
                      <div className="quartz-table-secondary">{row.detail}</div>
                    </td>
                    <td>
                      <span className={`quartz-status-badge ${row.statusTone}`}>
                        {row.isCurrent ? "In Focus" : row.phaseStatusLabel}
                      </span>
                    </td>
                    <td className="quartz-table-center">
                      <Link className="quartz-action-link" href={row.href}>
                        {resolvePrimaryWorkbenchLabel(row.phase)}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-kpi-label quartz-kpi-heading">Phase Progress</h2>
              </div>
              <div className="quartz-mini-list">
                {phaseRows.map((row) => (
                  <div className="quartz-mini-item" key={row.label}>
                    <div className="quartz-form-row">
                      <span className="quartz-table-secondary">{row.label}</span>
                      <span className="quartz-table-secondary">{row.statusLabel}</span>
                    </div>
                    <div className="quartz-progress-track">
                      <div
                        className="quartz-progress-bar"
                        style={{
                          background: row.color,
                          width: `${row.percent}%`,
                        }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </article>

            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-kpi-label quartz-kpi-heading">Lifecycle Timeline</h2>
              </div>
              <div className="quartz-mini-list">
                {timelineRows.map((row) => (
                  <div className="quartz-mini-item" key={row.id}>
                    <strong>{row.label}</strong>
                    <span className="quartz-mini-meta">{row.meta}</span>
                  </div>
                ))}
              </div>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <article className="quartz-card quartz-lifecycle-shell">
            <div className="quartz-section-header quartz-section-header-tight">
              <div>
                <p className="quartz-card-eyebrow">Lifecycle Control</p>
                <h3>Governed close-run actions</h3>
              </div>
              <p className="quartz-page-subtitle">{attention.detail}</p>
            </div>
            <div className="quartz-lifecycle-grid">
              <button
                className="quartz-lifecycle-action"
                disabled={isMutating || nextPhase === null}
                onClick={() => {
                  void handleAdvanceCloseRun();
                }}
                type="button"
              >
                <strong>
                  {isMutating
                    ? "Saving..."
                    : nextPhase === null
                      ? "No phase advance available"
                      : `Advance to ${getWorkflowPhaseDefinition(nextPhase).label}`}
                </strong>
                <span>
                  {nextPhase === null
                    ? "The workflow is already at its final governed phase."
                    : "Move the close run into the next controlled phase."}
                </span>
              </button>

              <button
                className="quartz-lifecycle-action"
                disabled={
                  isMutating || closeRun.status === "approved" || closeRun.status === "archived"
                }
                onClick={() => {
                  void handleApproveCloseRun();
                }}
                type="button"
              >
                <strong>{isMutating ? "Saving..." : "Approve Close Run"}</strong>
                <span>Finalize this run when all governed workstreams are complete.</span>
              </button>

              <button
                className="quartz-lifecycle-action"
                disabled={isMutating || closeRun.status === "archived"}
                onClick={() => {
                  void handleArchiveExistingCloseRun();
                }}
                type="button"
              >
                <strong>{isMutating ? "Saving..." : "Archive Close Run"}</strong>
                <span>Move the run into historical reference after operational use ends.</span>
              </button>

              <button
                className="quartz-lifecycle-action danger"
                disabled={isMutating || !canDeleteCloseRun(closeRun)}
                onClick={() => {
                  void handleDeleteCloseRun();
                }}
                type="button"
              >
                <strong>{isMutating ? "Saving..." : "Delete Mutable Close"}</strong>
                <span>Remove a still-mutable run and its linked working artifacts.</span>
              </button>
            </div>
          </article>
        </section>
      </section>
    </div>
  );
}

async function loadCloseRunWorkspace(options: {
  closeRunId: string;
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (value: CloseRunWorkspaceData) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const workspace = await readCloseRunWorkspace(options.entityId, options.closeRunId);
    options.onLoaded(workspace);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveCloseRunOverviewErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveCloseRunOverviewErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError || error instanceof CloseRunApiError) {
    return error.message;
  }

  return "The close-run overview could not be loaded. Reload the workspace and try again.";
}

function buildMissionTiles(closeRun: Readonly<CloseRunSummary>): readonly MissionTile[] {
  const activePhase = findActivePhase(closeRun);
  const completedPhases = closeRun.workflowState.phaseStates.filter(
    (phaseState) => phaseState.status === "completed",
  ).length;
  const blockedPhases = closeRun.workflowState.phaseStates.filter(
    (phaseState) => phaseState.status === "blocked",
  ).length;

  return [
    {
      label: "Close Status",
      meta: `Opened ${formatCloseRunDateTime(closeRun.createdAt)}`,
      value: getCloseRunStatusLabel(closeRun.status),
    },
    {
      label: "Current Gate",
      meta: `Version ${closeRun.currentVersionNo} • ${closeRun.reportingCurrency}`,
      value: activePhase ? getWorkflowPhaseDefinition(activePhase.phase).label : "Complete",
    },
    {
      label: "Completed Gates",
      meta:
        blockedPhases > 0 ? `${blockedPhases} blockers still active` : "No blocked workflow gates",
      tone: blockedPhases > 0 ? "error" : "success",
      value: `${completedPhases}/5`,
    },
  ];
}

function buildMissionWorkstreamRows(
  closeRun: Readonly<CloseRunSummary>,
  entityId: string,
): readonly MissionWorkstreamRow[] {
  const activePhase = findActivePhase(closeRun)?.phase ?? null;
  const phaseStateMap = new Map(
    closeRun.workflowState.phaseStates.map((phaseState) => [phaseState.phase, phaseState]),
  );

  return workflowPhaseOrder.map((phase) => {
    const phaseState = phaseStateMap.get(phase) ?? {
      blockingReason: null,
      completedAt: null,
      phase,
      status: activePhase === phase ? "in_progress" : ("not_started" as const),
    };

    return {
      detail: resolveWorkstreamDetail(phase, phaseState, closeRun),
      href: resolvePhaseWorkspaceHref(entityId, closeRun.id, phase),
      isBlocked: phaseState.status === "blocked",
      isCurrent: activePhase === phase,
      phase,
      phaseStatusLabel: getCloseRunPhaseStatusLabel(phaseState.status),
      statusTone: mapPhaseStatusToBadge(phaseState.status),
      title: resolvePhaseTitle(phase),
    };
  });
}

function buildPhaseProgressRows(closeRun: Readonly<CloseRunSummary>): readonly {
  color: string;
  label: string;
  percent: number;
  statusLabel: string;
}[] {
  return closeRun.workflowState.phaseStates.map((phaseState) => ({
    color: resolvePhaseColor(phaseState.status),
    label: getWorkflowPhaseDefinition(phaseState.phase).label,
    percent: resolvePhasePercent(phaseState.status),
    statusLabel: getCloseRunPhaseStatusLabel(phaseState.status),
  }));
}

function buildTimelineRows(closeRun: Readonly<CloseRunSummary>): readonly TimelineRow[] {
  const rows: TimelineRow[] = [
    {
      id: `${closeRun.id}-opened`,
      label: "Close run opened",
      meta: formatCloseRunDateTime(closeRun.createdAt),
    },
  ];

  closeRun.workflowState.phaseStates.forEach((phaseState) => {
    if (phaseState.completedAt === null) {
      return;
    }

    rows.push({
      id: `${closeRun.id}-${phaseState.phase}-completed`,
      label: `${getWorkflowPhaseDefinition(phaseState.phase).label} completed`,
      meta: formatCloseRunDateTime(phaseState.completedAt),
    });
  });

  if (closeRun.approvedAt !== null) {
    rows.push({
      id: `${closeRun.id}-approved`,
      label: "Close run approved",
      meta: formatCloseRunDateTime(closeRun.approvedAt),
    });
  }

  if (closeRun.archivedAt !== null) {
    rows.push({
      id: `${closeRun.id}-archived`,
      label: "Close run archived",
      meta: formatCloseRunDateTime(closeRun.archivedAt),
    });
  }

  return rows.sort((left, right) => right.meta.localeCompare(left.meta)).slice(0, 5);
}

function resolvePhaseWorkspaceHref(
  entityId: string,
  closeRunId: string,
  phase: WorkflowPhase,
): string {
  switch (phase) {
    case "collection":
      return `/entities/${entityId}/close-runs/${closeRunId}/documents`;
    case "processing":
      return `/entities/${entityId}/close-runs/${closeRunId}/recommendations`;
    case "reconciliation":
      return `/entities/${entityId}/close-runs/${closeRunId}/reconciliation`;
    case "reporting":
      return `/entities/${entityId}/close-runs/${closeRunId}/reports`;
    case "review_signoff":
      return `/entities/${entityId}/close-runs/${closeRunId}/exports`;
  }
}

function resolvePhaseTitle(phase: WorkflowPhase): string {
  switch (phase) {
    case "collection":
      return "Document Workspace";
    case "processing":
      return "Recommendations & Journals";
    case "reconciliation":
      return "Reconciliation";
    case "reporting":
      return "Reporting & Commentary";
    case "review_signoff":
      return "Sign-Off & Release";
  }
}

function resolvePrimaryWorkbenchLabel(phase: WorkflowPhase): string {
  switch (phase) {
    case "collection":
      return "Open Documents";
    case "processing":
      return "Review Journals";
    case "reconciliation":
      return "Open Reconciliation";
    case "reporting":
      return "Open Reports";
    case "review_signoff":
      return "Open Sign-Off";
  }
}

function resolveWorkstreamDetail(
  phase: WorkflowPhase,
  phaseState: Readonly<CloseRunSummary["workflowState"]["phaseStates"][number]>,
  closeRun: Readonly<CloseRunSummary>,
): string {
  if (phaseState.blockingReason !== null) {
    return phaseState.blockingReason;
  }

  switch (phaseState.status) {
    case "completed":
      return phaseState.completedAt
        ? `Completed ${formatCloseRunDateTime(phaseState.completedAt)}.`
        : "Completed and ready for historical reference.";
    case "ready":
      return "Gate checks are satisfied and the workflow can advance.";
    case "blocked":
      return "A reviewer action or missing dependency is preventing progress.";
    case "in_progress":
      return phase === "processing" && !closeRun.operatingMode.journalPostingAvailable
        ? "Review recommendations before journal posting is enabled for this run."
        : "This is the current working area for the period.";
    case "not_started":
      return getWorkflowPhaseDefinition(phase).description;
  }
}

function resolveNextWorkflowPhase(closeRun: Readonly<CloseRunSummary>): WorkflowPhase | null {
  const readyPhase = closeRun.workflowState.phaseStates.find(
    (phaseState) => phaseState.status === "ready",
  );
  if (readyPhase === undefined) {
    return null;
  }

  const currentIndex = workflowPhaseOrder.indexOf(readyPhase.phase);
  if (currentIndex < 0 || currentIndex === workflowPhaseOrder.length - 1) {
    return null;
  }

  return workflowPhaseOrder[currentIndex + 1] ?? null;
}

function canDeleteCloseRun(closeRun: Readonly<CloseRunSummary>): boolean {
  return (
    closeRun.status === "draft" || closeRun.status === "in_review" || closeRun.status === "reopened"
  );
}

function mapAttentionToneToBadge(
  tone: ReturnType<typeof deriveCloseRunAttention>["tone"],
): "error" | "success" | "warning" {
  switch (tone) {
    case "warning":
      return "error";
    case "success":
      return "success";
    default:
      return "warning";
  }
}

function mapPhaseStatusToBadge(
  status: CloseRunSummary["workflowState"]["phaseStates"][number]["status"],
): MissionWorkstreamRow["statusTone"] {
  switch (status) {
    case "completed":
    case "ready":
      return "success";
    case "blocked":
      return "error";
    case "in_progress":
      return "warning";
    case "not_started":
      return "neutral";
  }
}

function resolvePhaseColor(
  status: CloseRunSummary["workflowState"]["phaseStates"][number]["status"],
): string {
  switch (status) {
    case "completed":
      return "var(--quartz-success)";
    case "ready":
      return "var(--quartz-gold)";
    case "blocked":
      return "var(--quartz-error)";
    case "in_progress":
      return "var(--quartz-secondary)";
    case "not_started":
      return "var(--quartz-border)";
  }
}

function resolvePhasePercent(
  status: CloseRunSummary["workflowState"]["phaseStates"][number]["status"],
): number {
  switch (status) {
    case "completed":
      return 100;
    case "ready":
      return 84;
    case "blocked":
      return 62;
    case "in_progress":
      return 46;
    case "not_started":
      return 10;
  }
}
