/*
Purpose: Render the close-run overview page with lifecycle status, phase progress, and activity context.
Scope: Close-run workspace loading, overview metrics, phase progression, quick links into review surfaces, and related period navigation.
Dependencies: Same-origin close-run/entity API helpers and shared desktop UI components.
*/

"use client";

import {
  PhaseProgress,
  SurfaceCard,
  Timeline,
  type TimelineItem,
  type WorkflowPhase,
} from "@accounting-ai-agent/ui";
import Link from "next/link";
import { use, useEffect, useMemo, useState, type ReactElement } from "react";
import { AgentCapabilityCatalog } from "../../../../../../components/chat/AgentCapabilityCatalog";
import {
  approveCloseRun,
  archiveCloseRun,
  CloseRunApiError,
  buildPhaseProgressItems,
  deriveCloseRunAttention,
  findActivePhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  readCloseRunWorkspace,
  transitionCloseRun,
  type CloseRunSummary,
  type CloseRunWorkspaceData,
} from "../../../../../../lib/close-runs";
import { EntityApiError } from "../../../../../../lib/entities/api";

type CloseRunOverviewPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

/**
 * Purpose: Render the desktop overview surface for one entity close run.
 * Inputs: Route params containing the entity and close-run UUIDs.
 * Outputs: A client-rendered close-run overview with progress, activity, and queue-entry links.
 * Behavior: Hydrates the close-run, entity workspace, and sibling runs together so the page stays context-rich.
 */
export default function CloseRunOverviewPage({
  params,
}: Readonly<CloseRunOverviewPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isMutating, setIsMutating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<CloseRunWorkspaceData | null>(null);

  useEffect(() => {
    void loadCloseRunWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: setWorkspaceData,
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

  const relatedCloseRuns = useMemo(
    () =>
      workspaceData === null
        ? []
        : workspaceData.closeRuns
            .filter((closeRun) => closeRun.id !== workspaceData.closeRun.id)
            .slice(0, 4),
    [workspaceData],
  );

  const timelineItems = useMemo<readonly TimelineItem[]>(() => {
    if (workspaceData === null) {
      return [];
    }

    const closeRun = workspaceData.closeRun;
    const items: Array<{
      badge: string;
      detail: string;
      id: string;
      occurredAt: string;
      title: string;
      tone: NonNullable<TimelineItem["tone"]>;
    }> = [
      {
        badge: workspaceData.entity.name,
        detail: `Close run version ${closeRun.currentVersionNo} opened for ${formatCloseRunPeriod(closeRun)}.`,
        id: `${closeRun.id}-opened`,
        occurredAt: closeRun.createdAt,
        title: "Close run opened",
        tone: "default",
      },
    ];

    closeRun.workflowState.phaseStates.forEach((phaseState) => {
      if (phaseState.completedAt === null) {
        return;
      }

      items.push({
        badge: phaseState.phase.replaceAll("_", " "),
        detail: "The workflow advanced after this phase completed.",
        id: `${closeRun.id}-${phaseState.phase}-completed`,
        occurredAt: phaseState.completedAt,
        title: `${phaseState.phase.replaceAll("_", " ")} completed`,
        tone: "success",
      });
    });

    if (closeRun.approvedAt !== null) {
      items.push({
        badge: "Sign-off",
        detail: "The close run reached approved state and is ready for release controls.",
        id: `${closeRun.id}-approved`,
        occurredAt: closeRun.approvedAt,
        title: "Close run approved",
        tone: "success",
      });
    }

    if (closeRun.archivedAt !== null) {
      items.push({
        badge: "Archive",
        detail: "The close run was archived after release or review completion.",
        id: `${closeRun.id}-archived`,
        occurredAt: closeRun.archivedAt,
        title: "Close run archived",
        tone: "warning",
      });
    }

    return items
      .sort(
        (left, right) => new Date(right.occurredAt).valueOf() - new Date(left.occurredAt).valueOf(),
      )
      .map((item) => ({
        badge: item.badge,
        detail: item.detail,
        id: item.id,
        timestamp: formatCloseRunDateTime(item.occurredAt),
        title: item.title,
        tone: item.tone,
      }));
  }, [workspaceData]);

  if (isLoading) {
    return (
      <div className="app-shell close-run-overview-page">
        <SurfaceCard title="Loading Close Run" subtitle="Overview">
          <p className="form-helper">
            Loading close-run status, phase progress, and workspace context...
          </p>
        </SurfaceCard>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="app-shell close-run-overview-page">
        <SurfaceCard title="Close Run Unavailable" subtitle="Overview">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The requested close run could not be loaded."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  const closeRun = workspaceData.closeRun;
  const activePhase = findActivePhase(closeRun);
  const attention = deriveCloseRunAttention(closeRun);
  const nextPhase = getNextWorkflowPhase(closeRun);

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
        reason: "Advanced from close-run overview",
        target_phase: nextPhase,
      });
      setStatusMessage(`Close run advanced into ${formatWorkflowPhaseLabel(nextPhase)}.`);
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
      await approveCloseRun(entityId, closeRun.id, "Approved from close-run overview");
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
      await archiveCloseRun(entityId, closeRun.id, "Archived from close-run overview");
      setStatusMessage("Close run archived.");
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveCloseRunOverviewErrorMessage(error));
    } finally {
      setIsMutating(false);
    }
  }

  return (
    <div className="app-shell close-run-overview-page">
      <section className="hero-grid close-run-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Close Run Overview</p>
          <h1>{formatCloseRunPeriod(closeRun)}</h1>
          <p className="lede">
            Use this overview to move between workflow phases, review queues, and related entity
            activity without losing the current period context.
          </p>

          <div className="close-run-action-row">
            <Link className="secondary-button" href={`/entities/${workspaceData.entity.id}`}>
              Back to entity workspace
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/documents`}
            >
              Document queue
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/reconciliation`}
            >
              Reconciliation
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/recommendations`}
            >
              Recommendations
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/schedules`}
            >
              Supporting schedules
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/reports`}
            >
              Reporting
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/chat`}
            >
              Agent workbench
            </Link>
          </div>
        </div>

        <SurfaceCard title="Close-run Snapshot" subtitle={workspaceData.entity.name} tone="accent">
          <dl className="entity-meta-grid close-run-summary-grid">
            <div>
              <dt>Status</dt>
              <dd>{getCloseRunStatusLabel(closeRun.status)}</dd>
            </div>
            <div>
              <dt>Reporting currency</dt>
              <dd>{closeRun.reportingCurrency}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>v{closeRun.currentVersionNo}</dd>
            </div>
            <div>
              <dt>Active phase</dt>
              <dd>{activePhase ? activePhase.phase.replaceAll("_", " ") : "No active phase"}</dd>
            </div>
          </dl>
          <p className="form-helper">{attention.detail}</p>
        </SurfaceCard>
      </section>

      {statusMessage ? (
        <div className="status-banner success" role="status">
          {statusMessage}
        </div>
      ) : null}

      {errorMessage ? (
        <div className="status-banner warning" role="status">
          {errorMessage}
        </div>
      ) : null}

      <section className="close-run-stat-grid">
        <MetricCard label="Opened" value={formatCloseRunDateTime(closeRun.createdAt)} />
        <MetricCard label="Last updated" value={formatCloseRunDateTime(closeRun.updatedAt)} />
        <MetricCard label="Approved" value={formatCloseRunDateTime(closeRun.approvedAt)} />
        <MetricCard
          label="Source version"
          value={closeRun.reopenedFromCloseRunId ?? "Current root"}
        />
      </section>

      <section className="content-grid">
        <SurfaceCard title="Phase Progress" subtitle="Five-phase workflow">
          <PhaseProgress items={buildPhaseProgressItems(closeRun)} />
        </SurfaceCard>

        <SurfaceCard title="Lifecycle Controls" subtitle="Advance and sign off">
          <div className="dashboard-row-list">
            <article className="dashboard-row">
              <strong className="close-run-row-title">Advance workflow</strong>
              <p className="form-helper">
                Move this close run into the next canonical phase once the current gate is ready.
              </p>
              <div className="close-run-link-row">
                <button
                  className="secondary-button"
                  disabled={isMutating || nextPhase === null}
                  onClick={() => {
                    void handleAdvanceCloseRun();
                  }}
                  type="button"
                >
                  {isMutating
                    ? "Saving..."
                    : nextPhase === null
                      ? "No next phase"
                      : `Advance to ${formatWorkflowPhaseLabel(nextPhase)}`}
                </button>
              </div>
            </article>
            <article className="dashboard-row">
              <strong className="close-run-row-title">Approve close run</strong>
              <p className="form-helper">
                Sign off the period after reporting and review controls are satisfied.
              </p>
              <div className="close-run-link-row">
                <button
                  className="secondary-button"
                  disabled={isMutating || closeRun.status === "approved" || closeRun.status === "archived"}
                  onClick={() => {
                    void handleApproveCloseRun();
                  }}
                  type="button"
                >
                  {isMutating ? "Saving..." : "Approve"}
                </button>
              </div>
            </article>
            <article className="dashboard-row">
              <strong className="close-run-row-title">Archive close run</strong>
              <p className="form-helper">
                Archive a fully released period to lock it down for historical reference.
              </p>
              <div className="close-run-link-row">
                <button
                  className="secondary-button"
                  disabled={isMutating || closeRun.status === "archived"}
                  onClick={() => {
                    void handleArchiveExistingCloseRun();
                  }}
                  type="button"
                >
                  {isMutating ? "Saving..." : "Archive"}
                </button>
              </div>
            </article>
          </div>
        </SurfaceCard>

        <SurfaceCard title="Review Surfaces" subtitle="Jump into the work">
          <div className="dashboard-row-list">
            <QuickLinkRow
              description="Resolve collection blockers, low-confidence extractions, and wrong-period documents."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/documents`}
              label="Open document queue"
            />
            <QuickLinkRow
              description="Review accounting recommendations, generated journals, and reviewer decisions."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/recommendations`}
              label="Open accounting review"
            />
            <QuickLinkRow
              description="Disposition unmatched items, anomalies, and supporting schedule exceptions."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/reconciliation`}
              label="Open reconciliation review"
            />
            <QuickLinkRow
              description="Generate report packs, refine commentary, and inspect reporting artifacts."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/reports`}
              label="Open reporting workspace"
            />
            <QuickLinkRow
              description="Inspect background jobs, retries, and operational progress across this close run."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/jobs`}
              label="Open job monitor"
            />
            <QuickLinkRow
              description="Create exports, assemble evidence packs, and inspect the final release manifest."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/exports`}
              label="Open export center"
            />
            <QuickLinkRow
              description="Ask grounded questions about this period's source documents, rules, and outputs."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/chat`}
              label="Open agent workbench"
            />
            <QuickLinkRow
              description="Inspect and manage the active report template used by report generation."
              href={`/entities/${workspaceData.entity.id}/reports/templates`}
              label="Open report templates"
            />
            <QuickLinkRow
              description="Review entity-level setup, integrations, and chart of accounts context."
              href={`/entities/${workspaceData.entity.id}`}
              label="Open entity workspace"
            />
          </div>
        </SurfaceCard>
      </section>

      <section className="content-grid">
        <SurfaceCard title="Lifecycle Timeline" subtitle="Current period history">
          <Timeline
            emptyMessage="Lifecycle events will appear once this close run records phase completions or sign-off."
            items={timelineItems}
          />
        </SurfaceCard>

        <SurfaceCard title="Agent Capability Catalog" subtitle="Runtime visibility">
          <AgentCapabilityCatalog
            maxTools={8}
            workbenchHref={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/chat`}
          />
        </SurfaceCard>

        <SurfaceCard title="Related Periods" subtitle="Other close runs in this entity">
          <div className="dashboard-row-list">
            {relatedCloseRuns.length === 0 ? (
              <p className="form-helper">No other close runs exist for this entity yet.</p>
            ) : (
              relatedCloseRuns.map((relatedRun) => (
                <article className="dashboard-row" key={relatedRun.id}>
                  <div className="close-run-row-header">
                    <div>
                      <strong className="close-run-row-title">
                        {formatCloseRunPeriod(relatedRun)}
                      </strong>
                      <p className="close-run-row-meta">
                        {getCloseRunStatusLabel(relatedRun.status)} • v{relatedRun.currentVersionNo}
                      </p>
                    </div>
                    <span className="entity-status-chip">
                      {deriveCloseRunAttention(relatedRun).label}
                    </span>
                  </div>
                  <div className="close-run-link-row">
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${workspaceData.entity.id}/close-runs/${relatedRun.id}`}
                    >
                      Open overview
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${workspaceData.entity.id}/close-runs/${relatedRun.id}/documents`}
                    >
                      Documents
                    </Link>
                  </div>
                </article>
              ))
            )}
          </div>
        </SurfaceCard>
      </section>

      <SurfaceCard title="Management Report Workflow" subtitle="Client-facing workflow alignment">
        <div className="dashboard-row-list">
          {buildManagementReportWorkflowSteps(closeRun, workspaceData.entity.id).map((step) => (
            <article className="dashboard-row" key={step.id}>
              <div className="close-run-row-header">
                <div>
                  <strong className="close-run-row-title">
                    {step.stepNo}. {step.title}
                  </strong>
                  <p className="close-run-row-meta">{step.phaseLabel}</p>
                </div>
                <span className="entity-status-chip">{step.stateLabel}</span>
              </div>
              <p className="form-helper">{step.description}</p>
              <p className="form-helper">{step.detail}</p>
              <div className="close-run-link-row">
                <Link className="workspace-link-inline" href={step.href}>
                  Open workspace
                </Link>
              </div>
            </article>
          ))}
        </div>
      </SurfaceCard>
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

function MetricCard({
  label,
  value,
}: Readonly<{
  label: string;
  value: string;
}>): ReactElement {
  return (
    <article className="dashboard-stat-block">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function QuickLinkRow({
  description,
  href,
  label,
}: Readonly<{
  description: string;
  href: string;
  label: string;
}>): ReactElement {
  return (
    <article className="dashboard-row">
      <strong className="close-run-row-title">{label}</strong>
      <p className="form-helper">{description}</p>
      <div className="close-run-link-row">
        <Link className="workspace-link-inline" href={href}>
          Open
        </Link>
      </div>
    </article>
  );
}

function buildManagementReportWorkflowSteps(
  closeRun: Readonly<CloseRunSummary>,
  entityId: string,
): readonly {
  description: string;
  detail: string;
  href: string;
  id: string;
  phaseLabel: string;
  stateLabel: string;
  stepNo: string;
  title: string;
}[] {
  const phaseStates = new Map(
    closeRun.workflowState.phaseStates.map((phaseState) => [phaseState.phase, phaseState]),
  );
  const activePhase = closeRun.workflowState.activePhase;

  return [
    {
      description: "Bank statements, invoices, payslips, receipts, and contracts.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/documents`,
      phase: "collection" as WorkflowPhase,
      stepNo: "01",
      title: "Collect source documents",
    },
    {
      description:
        "Check completeness, authorization, correct period, and transaction matching before processing.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/documents`,
      phase: "collection" as WorkflowPhase,
      stepNo: "02",
      title: "Review and verify documents",
    },
    {
      description: "Assign GL account codes, cost centres, departments, and projects.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/recommendations`,
      phase: "processing" as WorkflowPhase,
      stepNo: "03",
      title: "Code and classify transactions",
    },
    {
      description: "Record journals, accruals, prepayments, and depreciation entries.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/recommendations`,
      phase: "processing" as WorkflowPhase,
      stepNo: "04",
      title: "Post transactions to the General Ledger",
    },
    {
      description: "Reconcile bank, AR/AP ageing, intercompany, and payroll control balances.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/reconciliation`,
      phase: "reconciliation" as WorkflowPhase,
      stepNo: "05",
      title: "Reconcile key accounts",
    },
    {
      description: "Update fixed assets, loan amortisation, accrual tracker, and budget-vs-actual schedules.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/schedules`,
      phase: "reconciliation" as WorkflowPhase,
      stepNo: "06",
      title: "Update supporting schedules",
    },
    {
      description: "Confirm debits equal credits and clear unexplained anomalies or variances.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/reconciliation`,
      phase: "reconciliation" as WorkflowPhase,
      stepNo: "07",
      title: "Run and review trial balance",
    },
    {
      description: "Generate the management report pack with P&L, Balance Sheet, Cash Flow, variance, and KPI outputs.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/reports`,
      phase: "reporting" as WorkflowPhase,
      stepNo: "08",
      title: "Prepare management report",
    },
    {
      description: "Write and approve commentary covering variances, performance, risks, and management actions.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/reports`,
      phase: "reporting" as WorkflowPhase,
      stepNo: "09",
      title: "Write commentary and analysis",
    },
    {
      description: "Finalize review, sign-off, export packaging, evidence pack coverage, and management distribution.",
      href: `/entities/${entityId}/close-runs/${closeRun.id}/exports`,
      phase: "review_signoff" as WorkflowPhase,
      stepNo: "10",
      title: "Review, sign-off, and distribute",
    },
  ].map((step) => {
    const phaseState = phaseStates.get(step.phase);
    const stateLabel =
      phaseState?.status === "completed"
        ? "Completed"
        : activePhase === step.phase
          ? phaseState?.blockingReason
            ? "Blocked"
            : "Active"
          : "Upcoming";
    const detail =
      phaseState?.status === "completed"
        ? "Phase completed for this close run."
        : phaseState?.blockingReason ??
          (activePhase === step.phase
            ? "This is the current active workflow phase."
            : "This step becomes available after earlier phases are completed.");
    return {
      description: step.description,
      detail,
      href: step.href,
      id: `${closeRun.id}:${step.stepNo}`,
      phaseLabel: phaseState?.phase.replaceAll("_", " ") ?? step.phase.replaceAll("_", " "),
      stateLabel,
      stepNo: step.stepNo,
      title: step.title,
    };
  });
}

const WORKFLOW_PHASE_ORDER: readonly WorkflowPhase[] = [
  "collection",
  "processing",
  "reconciliation",
  "reporting",
  "review_signoff",
];

function getNextWorkflowPhase(closeRun: Readonly<CloseRunSummary>): WorkflowPhase | null {
  const activePhase = closeRun.workflowState.activePhase;
  if (activePhase === null) {
    return null;
  }
  const activeIndex = WORKFLOW_PHASE_ORDER.indexOf(activePhase);
  if (activeIndex < 0 || activeIndex === WORKFLOW_PHASE_ORDER.length - 1) {
    return null;
  }
  return WORKFLOW_PHASE_ORDER[activeIndex + 1] ?? null;
}

function formatWorkflowPhaseLabel(phase: WorkflowPhase): string {
  return phase.replaceAll("_", " ");
}
