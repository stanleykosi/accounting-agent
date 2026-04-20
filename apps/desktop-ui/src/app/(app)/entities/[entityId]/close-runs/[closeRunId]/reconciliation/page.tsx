/*
Purpose: Render the reconciliation review workspace for one entity close run.
Scope: Queue loading, filter and selection state, side-by-side match review table,
       disposition panel, anomaly list, and trial balance summary.
Dependencies: Reconciliation API helpers, review components, and shared UI surface primitives.
*/

"use client";

import { EvidenceDrawer, ReviewLayout, SurfaceCard } from "@accounting-ai-agent/ui";
import type { EvidenceDrawerReference } from "@accounting-ai-agent/ui";
import { use, useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import Link from "next/link";
import { DispositionPanel } from "../../../../../../../components/reconciliation/DispositionPanel";
import { MatchReviewTable } from "../../../../../../../components/reconciliation/MatchReviewTable";
import {
  approveReconciliation,
  type DispositionActionValue,
  type ReconciliationAnomalySummary,
  type ReconciliationRunResponse,
  type ReconciliationReviewFilter,
  type ReconciliationReviewWorkspaceData,
  filterReconciliationItems,
  formatReconciliationTypeLabel,
  getSeverityColor,
  readReconciliationReviewWorkspace,
  resolveAnomaly,
  runReconciliation,
  submitDispositionItem,
  ReconciliationApiError,
} from "../../../../../../../lib/reconciliation";

type CloseRunReconciliationPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

type EvidenceDrawerState = {
  isOpen: boolean;
  references: readonly EvidenceDrawerReference[];
  sourceLabel: string;
  title: string;
};

const defaultEvidenceDrawerState: EvidenceDrawerState = {
  isOpen: false,
  references: [],
  sourceLabel: "Evidence",
  title: "Evidence references",
};

/**
 * Purpose: Compose the reconciliation review workspace for one entity close run.
 * Inputs: Route params containing entity and close-run UUIDs.
 * Outputs: A client-rendered review workspace with match review table, disposition panel,
 *          anomaly list, and trial balance summary.
 * Behavior: Loads workspace state from same-origin API routes and keeps reviewer decisions
 *           local to the active page session.
 */
export default function CloseRunReconciliationPage({
  params,
}: Readonly<CloseRunReconciliationPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);

  const [activeFilter, setActiveFilter] = useState<ReconciliationReviewFilter>("all");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<ReconciliationReviewWorkspaceData | null>(null);
  const [evidenceDrawer, setEvidenceDrawer] = useState<EvidenceDrawerState>(defaultEvidenceDrawerState);
  const [isQueueingRun, setIsQueueingRun] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isApprovingReconciliationId, setIsApprovingReconciliationId] = useState<string | null>(null);
  const [queuedRun, setQueuedRun] = useState<ReconciliationRunResponse | null>(null);
  const [resolutionNotes, setResolutionNotes] = useState<Record<string, string>>({});

  const refreshWorkspace = useCallback(async (): Promise<void> => {
    await loadWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: (nextWorkspace) => {
        setWorkspaceData(nextWorkspace);
        setSelectedItemId(selectInitialItemId(nextWorkspace));
      },
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

  useEffect(() => {
    void refreshWorkspace();
  }, [refreshWorkspace]);

  const handleRefreshWorkspace = useCallback(async (): Promise<void> => {
    setIsRefreshing(true);
    try {
      await refreshWorkspace();
    } finally {
      setIsRefreshing(false);
    }
  }, [refreshWorkspace]);

  const handleApproveReconciliation = useCallback(
    async (reconciliationId: string): Promise<void> => {
      setIsApprovingReconciliationId(reconciliationId);
      try {
        await approveReconciliation(
          entityId,
          closeRunId,
          reconciliationId,
          "Approved in reconciliation workspace",
        );
        setErrorMessage(null);
        await refreshWorkspace();
      } catch (error: unknown) {
        setErrorMessage(resolveReconciliationErrorMessage(error));
      } finally {
        setIsApprovingReconciliationId(null);
      }
    },
    [closeRunId, entityId, refreshWorkspace],
  );

  const visibleItems = useMemo(
    () =>
      workspaceData === null
        ? []
        : filterReconciliationItems(workspaceData.items, activeFilter),
    [activeFilter, workspaceData],
  );

  const selectedItem = useMemo(() => {
    if (workspaceData === null || selectedItemId === null) {
      return null;
    }
    return workspaceData.items.find((item) => item.id === selectedItemId) ?? null;
  }, [selectedItemId, workspaceData]);

  const unresolvedAnomalies = useMemo(
    () => workspaceData?.anomalies.filter((a) => !a.resolved) ?? [],
    [workspaceData],
  );

  const handleFilterChange = useCallback((filter: ReconciliationReviewFilter): void => {
    setActiveFilter(filter);
  }, []);

  const handleSelectItem = useCallback((itemId: string): void => {
    setSelectedItemId(itemId);
  }, []);

  const handleOpenEvidence = useCallback((itemId: string): void => {
    if (workspaceData === null) {
      return;
    }
    const item = workspaceData.items.find((i) => i.id === itemId);
    if (!item) {
      return;
    }
    setSelectedItemId(item.id);
    setEvidenceDrawer({
      isOpen: true,
      references: buildEvidenceRefsFromItem(item),
      sourceLabel: item.sourceRef,
      title: "Reconciliation evidence",
    });
  }, [workspaceData]);

  const handleDisposition = useCallback(
    async (itemId: string, disposition: DispositionActionValue, reason: string): Promise<void> => {
      if (workspaceData === null) {
        throw new Error("Workspace data not loaded.");
      }
      await submitDispositionItem(entityId, closeRunId, itemId, disposition, reason);
      // Refresh workspace to reflect the disposition
      await refreshWorkspace();
    },
    [workspaceData, entityId, closeRunId, refreshWorkspace],
  );

  const handleResolveAnomaly = useCallback(
    async (anomalyId: string): Promise<void> => {
      const note = resolutionNotes[anomalyId]?.trim();
      if (!note) {
        return;
      }
      await resolveAnomaly(entityId, closeRunId, anomalyId, note);
      setResolutionNotes((prev) => ({ ...prev, [anomalyId]: "" }));
      await refreshWorkspace();
    },
    [entityId, closeRunId, refreshWorkspace, resolutionNotes],
  );

  const handleRunReconciliation = useCallback(async (): Promise<void> => {
    setIsQueueingRun(true);
    try {
      const result = await runReconciliation(entityId, closeRunId);
      setQueuedRun(result);
      setErrorMessage(null);
      window.setTimeout(() => {
        void refreshWorkspace();
      }, 1500);
    } catch (error: unknown) {
      setErrorMessage(resolveReconciliationErrorMessage(error));
    } finally {
      setIsQueueingRun(false);
    }
  }, [closeRunId, entityId, refreshWorkspace]);

  if (isLoading) {
    return (
      <div className="app-shell reconciliation-review-page">
        <SurfaceCard title="Loading Reconciliation Review" subtitle="Reconciliation phase">
          <p className="form-helper">
            Loading reconciliation review workspace, match results, and trial balance...
          </p>
        </SurfaceCard>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="app-shell reconciliation-review-page">
        <SurfaceCard title="Reconciliation Review Unavailable" subtitle="Reconciliation phase">
          <div className="status-banner danger" role="alert">
            {errorMessage ??
              "The reconciliation review workspace could not be loaded. Verify the entity and close-run IDs, then retry."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  return (
    <div className="app-shell reconciliation-review-page">
      {/* Hero section */}
      <section className="hero-grid reconciliation-review-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Reconciliation Review</p>
          <h1>Match results, exceptions, and trial balance review.</h1>
          <p className="lede">
            Resolve unmatched items, investigate anomalies, and validate the trial balance before
            the close run advances from Reconciliation into Reporting.
          </p>
        </div>

        <SurfaceCard title="Close-run Context" subtitle="Reconciliation phase" tone="accent">
          <dl className="entity-meta-grid reconciliation-summary-grid">
            <div>
              <dt>Close run</dt>
              <dd>{workspaceData.closeRunId}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{workspaceData.closeRunStatus.replaceAll("_", " ")}</dd>
            </div>
            <div>
              <dt>Operating mode</dt>
              <dd>{formatOperatingModeLabel(workspaceData.closeRunOperatingMode)}</dd>
            </div>
            <div>
              <dt>Reconciliation types</dt>
              <dd>
                {workspaceData.reconciliations.length > 0
                  ? workspaceData.reconciliations
                      .map((r) => formatReconciliationTypeLabel(r.reconciliationType))
                      .join(", ")
                  : "None run yet"}
              </dd>
            </div>
          </dl>

          <div className="document-metric-row">
            <MetricChip label="Unresolved" value={workspaceData.queueCounts.unresolved} />
            <MetricChip label="Matched" value={workspaceData.queueCounts.matched} />
            <MetricChip label="Exceptions" value={workspaceData.queueCounts.exception} />
            <MetricChip label="Unmatched" value={workspaceData.queueCounts.unmatched} />
            <MetricChip label="Unresolved anomalies" value={workspaceData.queueCounts.anomalyUnresolved} />
          </div>

          <p className="form-helper" style={{ marginTop: "16px" }}>
            {workspaceData.closeRunOperatingModeDescription ??
              "The close run automatically adapts reconciliation behavior to the data currently available."}
          </p>

          <div className="integration-action-stack" style={{ marginTop: "16px" }}>
            <div className="close-run-link-row">
              <button
                className="primary-button"
                disabled={isQueueingRun}
                onClick={() => {
                  void handleRunReconciliation();
                }}
                type="button"
              >
                {isQueueingRun ? "Queueing..." : "Run reconciliation"}
              </button>
              <button
                className="secondary-button"
                disabled={isRefreshing}
                onClick={() => {
                  void handleRefreshWorkspace();
                }}
                type="button"
              >
                {isRefreshing ? "Refreshing..." : "Refresh workspace"}
              </button>
            </div>
            <p className="form-helper">
              Queue the full reconciliation engine for this period. Once the runs appear below,
              clear any exceptions and approve each reconciliation run to unblock Reporting.
            </p>
          </div>
        </SurfaceCard>
      </section>

      {queuedRun ? (
        <div
          className={`status-banner ${queuedRun.status === "not_applicable" ? "warning" : "success"}`}
          role="status"
        >
          {queuedRun.status === "not_applicable"
            ? (queuedRun.message ?? "No applicable reconciliation work was detected for this run.")
            : `Reconciliation job queued: ${queuedRun.job_id}${queuedRun.message ? ` — ${queuedRun.message}` : ""}`}
        </div>
      ) : null}

      <SurfaceCard title="Reconciliation Runs" subtitle={`${workspaceData.reconciliations.length} run(s)`}>
        {workspaceData.reconciliations.length === 0 ? (
          <p className="form-helper">
            No reconciliation runs exist yet. Click <strong>Run reconciliation</strong>, wait for
            the worker to finish, then refresh this workspace.
          </p>
        ) : (
          <div className="dashboard-row-list">
            {workspaceData.reconciliations.map((reconciliation) => (
              <article className="dashboard-row" key={reconciliation.id}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">
                      {formatReconciliationTypeLabel(reconciliation.reconciliationType)}
                    </strong>
                    <p className="close-run-row-meta">
                      {reconciliation.status.replaceAll("_", " ")} • {reconciliation.itemCount} item(s)
                    </p>
                  </div>
                </div>
                <p className="form-helper">
                  {reconciliation.blockingReason ??
                    (reconciliation.status === "approved"
                      ? "This reconciliation run is approved."
                      : "Review the generated queue and approve this run when its exceptions are resolved.")}
                </p>
                <div className="close-run-link-row">
                  <button
                    className="secondary-button"
                    disabled={
                      reconciliation.status === "approved" ||
                      isApprovingReconciliationId === reconciliation.id
                    }
                    onClick={() => {
                      void handleApproveReconciliation(reconciliation.id);
                    }}
                    type="button"
                  >
                    {isApprovingReconciliationId === reconciliation.id
                      ? "Approving..."
                      : reconciliation.status === "approved"
                        ? "Approved"
                        : "Approve run"}
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </SurfaceCard>

      {workspaceData.reconciliations.length === 0 &&
      workspaceData.items.length === 0 &&
      workspaceData.anomalies.length === 0 &&
      workspaceData.trialBalance === null ? (
        <div className="status-banner info" role="status">
          {workspaceData.bankReconciliationAvailable || workspaceData.trialBalanceReviewAvailable
            ? "No reconciliation review items exist yet. Run reconciliation to generate the current control queues for this period."
            : "No reconciliation review items exist yet. This run does not currently have ledger-side data, so detailed bank reconciliation is not applicable. You can advance to Reporting when the rest of the run is ready."}
        </div>
      ) : null}

      <SurfaceCard title="Phase 3 Workflow Coverage" subtitle="Steps 05, 06, and 07">
        <div className="dashboard-row-list">
          <article className="dashboard-row">
            <strong className="close-run-row-title">05. Reconcile key accounts</strong>
            <p className="form-helper">
              {workspaceData.bankReconciliationAvailable
                ? "Bank reconciliation and other key balance controls run through this workspace and its exception queue."
                : "Detailed bank reconciliation is not currently applicable because this run has no ledger-side data to match against. This workspace still handles applicable schedules and control checks."}
            </p>
          </article>
          <article className="dashboard-row">
            <strong className="close-run-row-title">06. Update supporting schedules</strong>
            <p className="form-helper">
              Fixed asset register, loan amortisation, accrual tracker, and budget-vs-actual
              workpapers are now maintained in a dedicated Step 06 editor and then fed into this
              reconciliation workspace.
            </p>
          </article>
          <article className="dashboard-row">
            <strong className="close-run-row-title">07. Run and review trial balance</strong>
            <p className="form-helper">
              {workspaceData.trialBalanceReviewAvailable
                ? "Debits versus credits, anomalies, and unexplained variances must be cleared here before the close run can advance into Reporting."
                : "Trial-balance review becomes applicable once a trial-balance baseline or ledger-side transaction set exists for the run."}
            </p>
          </article>
        </div>
      </SurfaceCard>

      <SurfaceCard title="Supporting Schedules" subtitle="Step 06 schedule coverage">
        <div className="close-run-action-row" style={{ marginBottom: "16px" }}>
          <Link
            className="secondary-button"
            href={`/entities/${entityId}/close-runs/${closeRunId}/schedules`}
          >
            Open Step 06 editor
          </Link>
        </div>
        <div className="dashboard-row-list">
          {[
            {
              label: "Fixed asset register",
              reconciliationType: "fixed_assets",
            },
            {
              label: "Loan amortisation",
              reconciliationType: "loan_amortisation",
            },
            {
              label: "Accrual tracker",
              reconciliationType: "accrual_tracker",
            },
            {
              label: "Budget vs actual",
              reconciliationType: "budget_vs_actual",
            },
          ].map((schedule) => {
            const run =
              workspaceData.reconciliations.find(
                (reconciliation) =>
                  reconciliation.reconciliationType === schedule.reconciliationType,
              ) ?? null;
            return (
              <article className="dashboard-row" key={schedule.reconciliationType}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">{schedule.label}</strong>
                    <p className="close-run-row-meta">
                      {run === null ? "Not run yet" : formatReconciliationTypeLabel(run.reconciliationType)}
                    </p>
                  </div>
                  <span className="close-run-row-meta">
                    {run === null ? "Pending" : run.status.replaceAll("_", " ")}
                  </span>
                </div>
                <p className="form-helper">
                  {run === null
                    ? "Maintain this workpaper in the Step 06 editor, then run reconciliation to surface exceptions."
                    : run.blockingReason ?? "Schedule checks are available in the reconciliation review queue."}
                </p>
                <div className="close-run-link-row">
                  <Link
                    className="workspace-link-inline"
                    href={`/entities/${entityId}/close-runs/${closeRunId}/schedules`}
                  >
                    Open editor
                  </Link>
                </div>
              </article>
            );
          })}
        </div>
      </SurfaceCard>

      {/* Trial balance summary */}
      {workspaceData.trialBalance && (
        <SurfaceCard title="Trial Balance" subtitle={`Snapshot #${workspaceData.trialBalance.snapshotNo}`}>
          <dl className="trial-balance-grid">
            <div>
              <dt>Total Debits</dt>
              <dd>{workspaceData.trialBalance.totalDebits}</dd>
            </div>
            <div>
              <dt>Total Credits</dt>
              <dd>{workspaceData.trialBalance.totalCredits}</dd>
            </div>
            <div>
              <dt>Accounts</dt>
              <dd>{workspaceData.trialBalance.accountCount}</dd>
            </div>
            <div>
              <dt>Balance Status</dt>
              <dd>
                <span
                  className={`balance-status ${workspaceData.trialBalance.isBalanced ? "balanced" : "unbalanced"}`}
                >
                  {workspaceData.trialBalance.isBalanced ? "Balanced" : "Unbalanced"}
                </span>
              </dd>
            </div>
          </dl>
        </SurfaceCard>
      )}

      {/* Error banner */}
      {errorMessage && (
        <div className="status-banner warning" role="status">
          {errorMessage}
        </div>
      )}

      {/* Anomalies */}
      {unresolvedAnomalies.length > 0 && (
        <SurfaceCard title="Unresolved Anomalies" subtitle={`${unresolvedAnomalies.length} items requiring investigation`}>
          <div className="anomaly-list">
            {unresolvedAnomalies.map((anomaly) => (
              <AnomalyRow
                key={anomaly.id}
                anomaly={anomaly}
                resolutionNote={resolutionNotes[anomaly.id] ?? ""}
                onNoteChange={(note) =>
                  setResolutionNotes((prev) => ({ ...prev, [anomaly.id]: note }))
                }
                onResolve={() => void handleResolveAnomaly(anomaly.id)}
              />
            ))}
          </div>
        </SurfaceCard>
      )}

      <ReviewLayout
        className="reconciliation-review-grid"
        main={
          <SurfaceCard title="Match Review Queue" subtitle={`${visibleItems.length} items`}>
            <MatchReviewTable
              activeFilter={activeFilter}
              items={visibleItems}
              queueCounts={workspaceData.queueCounts}
              onFilterChange={handleFilterChange}
              onSelectItem={handleSelectItem}
              onOpenEvidence={handleOpenEvidence}
              onReviewAction={() => {}}
              selectedItemId={selectedItemId}
            />
          </SurfaceCard>
        }
        side={
          <div className="reconciliation-review-side-column">
            <SurfaceCard title="Disposition Panel" subtitle="Selected item">
              <DispositionPanel
                selectedItem={selectedItem}
                onDisposition={handleDisposition}
                onOpenEvidence={handleOpenEvidence}
              />
            </SurfaceCard>

            <SurfaceCard title="Evidence Drawer" subtitle="Source-backed references">
              <EvidenceDrawer
                emptyMessage="Select a queue row to open source-backed evidence references."
                isOpen={evidenceDrawer.isOpen}
                onClose={() => setEvidenceDrawer(defaultEvidenceDrawerState)}
                references={evidenceDrawer.references}
                sourceLabel={evidenceDrawer.sourceLabel}
                title={evidenceDrawer.title}
              />
              {!evidenceDrawer.isOpen ? (
                <p className="form-helper">
                  Open evidence from the queue to inspect source metadata and confidence traces.
                </p>
              ) : null}
            </SurfaceCard>
          </div>
        }
      />
    </div>
  );
}

/**
 * Purpose: Render one anomaly row with resolution controls.
 * Inputs: Anomaly record, resolution note state, and handlers.
 * Outputs: A row showing anomaly type, severity, description, and a resolve form.
 */
function AnomalyRow({
  anomaly,
  resolutionNote,
  onNoteChange,
  onResolve,
}: Readonly<{
  anomaly: ReconciliationAnomalySummary;
  resolutionNote: string;
  onNoteChange: (note: string) => void;
  onResolve: () => void;
}>): ReactElement {
  return (
    <div className="anomaly-row-card">
      <div className="anomaly-header">
        <span
          className="anomaly-severity-badge"
          style={{ backgroundColor: getSeverityColor(anomaly.severity), color: "#fff" }}
        >
          {anomaly.severity.toUpperCase()}
        </span>
        <span className="anomaly-type-label">{anomaly.anomalyType.replaceAll("_", " ")}</span>
        {anomaly.accountCode && (
          <span className="anomaly-account-label">Account: {anomaly.accountCode}</span>
        )}
      </div>
      <p className="anomaly-description">{anomaly.description}</p>
      <div className="anomaly-resolve-row">
        <input
          type="text"
          className="text-input anomaly-resolve-input"
          placeholder="Resolution note..."
          value={resolutionNote}
          onChange={(e) => onNoteChange(e.target.value)}
          maxLength={500}
        />
        <button
          className="primary-button compact-action"
          onClick={onResolve}
          disabled={resolutionNote.trim().length === 0}
          type="button"
        >
          Resolve
        </button>
      </div>
    </div>
  );
}

/**
 * Purpose: Fetch and hydrate the reconciliation review workspace state.
 * Inputs: Route identifiers and page-level state update callbacks.
 * Outputs: None; callers receive deterministic state updates through provided callbacks.
 */
async function loadWorkspace(options: {
  closeRunId: string;
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: ReconciliationReviewWorkspaceData) => void;
  onLoadingChange: (isLoading: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  options.onError(null);

  try {
    const workspace = await readReconciliationReviewWorkspace(options.entityId, options.closeRunId);
    options.onLoaded(workspace);
  } catch (error: unknown) {
    if (error instanceof ReconciliationApiError) {
      options.onError(error.message);
    } else {
      options.onError("Failed to load the reconciliation review workspace. Reload and try again.");
    }
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveReconciliationErrorMessage(error: unknown): string {
  if (error instanceof ReconciliationApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "The reconciliation action failed. Retry the request.";
}

function formatOperatingModeLabel(mode: string): string {
  switch (mode) {
    case "working_ledger":
      return "Working ledger";
    case "imported_general_ledger":
      return "Imported general ledger";
    case "trial_balance_only":
      return "Trial balance only";
    default:
      return "Source documents only";
  }
}

/**
 * Purpose: Pick a stable initial selection for the queue details pane.
 * Inputs: Fully loaded workspace data.
 * Outputs: The item ID that should be focused first, or null when the queue is empty.
 * Behavior: Prioritizes unresolved items so reviewers immediately land on actionable items.
 */
function selectInitialItemId(workspace: ReconciliationReviewWorkspaceData): string | null {
  return (
    workspace.items.find((item) => item.requiresDisposition && item.disposition === null)?.id ??
    workspace.items.find((item) => item.matchStatus === "exception")?.id ??
    workspace.items[0]?.id ??
    null
  );
}

/**
 * Purpose: Build evidence references from a reconciliation item's matched counterparts.
 * Inputs: A reconciliation item summary.
 * Outputs: An array of evidence reference objects for the EvidenceDrawer.
 */
function buildEvidenceRefsFromItem(item: {
  sourceRef: string;
  matchedTo: ReadonlyArray<{ sourceType: string; sourceRef: string; confidence: number | null }>;
}): EvidenceDrawerReference[] {
  const refs: EvidenceDrawerReference[] = [];
  refs.push({
    id: `source-${item.sourceRef}`,
    label: "Source Record",
    kind: "source",
    location: item.sourceRef,
    snippet: `Source: ${item.sourceRef}`,
  });
  for (const cp of item.matchedTo) {
    refs.push({
      id: `match-${cp.sourceRef}`,
      label: `Match: ${cp.sourceType}`,
      kind: "match",
      location: cp.sourceRef,
      snippet: `Matched to ${cp.sourceRef}`,
      confidence: cp.confidence ?? null,
    });
  }
  return refs;
}

/**
 * Purpose: Render a compact numeric metric chip for queue summary cards.
 * Inputs: Metric label and integer count value.
 * Outputs: A short inline metric element used in the close-run context card.
 */
function MetricChip({
  label,
  value,
}: Readonly<{
  label: string;
  value: number;
}>): ReactElement {
  return (
    <div className="document-metric-chip">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
