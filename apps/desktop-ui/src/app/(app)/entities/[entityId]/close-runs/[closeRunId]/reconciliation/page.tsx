/*
Purpose: Render the Quartz reconciliation workspace for one close run.
Scope: Run execution, exception triage, quick disposition actions, anomaly review,
       evidence access, and assistant-guided control analysis.
Dependencies: Close-run context, reconciliation review APIs, job polling, and
              the shared Quartz workspace styles.
*/

"use client";

import { EvidenceDrawer } from "@accounting-ai-agent/ui";
import type { EvidenceDrawerReference } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../../../../../components/layout/QuartzIcons";
import {
  CloseRunApiError,
  formatCloseRunPeriod,
  readCloseRunWorkspace,
  type CloseRunWorkspaceData,
} from "../../../../../../../lib/close-runs";
import {
  type JobDetail,
  JobApiError,
  listEntityJobs,
  readJobDetail,
} from "../../../../../../../lib/jobs";
import {
  approveReconciliation,
  filterReconciliationItems,
  formatMatchStatusLabel,
  formatReconciliationTypeLabel,
  readReconciliationReviewWorkspace,
  type ReconciliationItemSummary,
  type ReconciliationReviewFilter,
  type ReconciliationReviewWorkspaceData,
  type ReconciliationRunResponse,
  ReconciliationApiError,
  resolveAnomaly,
  runReconciliation,
  submitDispositionItem,
} from "../../../../../../../lib/reconciliation";
import { requireRouteParam } from "../../../../../../../lib/route-params";

type EvidenceDrawerState = {
  isOpen: boolean;
  references: readonly EvidenceDrawerReference[];
  sourceLabel: string;
  title: string;
};

type ReconciliationPageData = {
  closeRunWorkspace: CloseRunWorkspaceData;
  reviewWorkspace: ReconciliationReviewWorkspaceData;
};

const defaultEvidenceDrawerState: EvidenceDrawerState = {
  isOpen: false,
  references: [],
  sourceLabel: "Evidence",
  title: "Evidence references",
};

const reconciliationFilters: readonly {
  filter: ReconciliationReviewFilter;
  label: string;
}[] = [
  { filter: "all", label: "All Items" },
  { filter: "unresolved", label: "Needs Action" },
  { filter: "exception", label: "Exceptions" },
  { filter: "unmatched", label: "Unmatched" },
  { filter: "matched", label: "Matched" },
];

export default function CloseRunReconciliationPage(): ReactElement {
  const routeParams = useParams<{ closeRunId: string; entityId: string }>();
  const closeRunId = requireRouteParam(routeParams.closeRunId, "closeRunId");
  const entityId = requireRouteParam(routeParams.entityId, "entityId");

  const [activeActionKey, setActiveActionKey] = useState<string | null>(null);
  const [activeFilter, setActiveFilter] = useState<ReconciliationReviewFilter>("all");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [evidenceDrawer, setEvidenceDrawer] = useState<EvidenceDrawerState>(
    defaultEvidenceDrawerState,
  );
  const [isLoading, setIsLoading] = useState(true);
  const [isQueueingRun, setIsQueueingRun] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [jobErrorMessage, setJobErrorMessage] = useState<string | null>(null);
  const [queuedRun, setQueuedRun] = useState<ReconciliationRunResponse | null>(null);
  const [reconciliationJob, setReconciliationJob] = useState<JobDetail | null>(null);
  const [resolutionNotes, setResolutionNotes] = useState<Record<string, string>>({});
  const [selectedItemId, setSelectedItemId] = useState<string | null>(null);
  const [selectedItemNote, setSelectedItemNote] = useState("");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<ReconciliationPageData | null>(null);
  const handledTerminalJobKeyRef = useRef<string | null>(null);

  const refreshWorkspace = useCallback(async (): Promise<void> => {
    setIsLoading(true);
    try {
      const [closeRunWorkspace, reviewWorkspace] = await Promise.all([
        readCloseRunWorkspace(entityId, closeRunId),
        readReconciliationReviewWorkspace(entityId, closeRunId),
      ]);

      setWorkspaceData({
        closeRunWorkspace,
        reviewWorkspace,
      });
      setSelectedItemId((currentSelectedItemId) => {
        if (
          currentSelectedItemId !== null &&
          reviewWorkspace.items.some((item) => item.id === currentSelectedItemId)
        ) {
          return currentSelectedItemId;
        }

        return selectInitialItemId(reviewWorkspace);
      });
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveReconciliationErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId]);

  useEffect(() => {
    void refreshWorkspace();
  }, [refreshWorkspace]);

  useEffect(() => {
    setSelectedItemNote("");
  }, [selectedItemId]);

  const refreshReconciliationJob = useCallback(
    async (preferredJobId?: string | null): Promise<JobDetail | null> => {
      try {
        const jobs = await listEntityJobs(entityId, { closeRunId });
        const reconciliationJobs = jobs.filter(
          (job) => job.task_name === "reconciliation.execute_close_run",
        );
        const selectedJob =
          (preferredJobId ? reconciliationJobs.find((job) => job.id === preferredJobId) : null) ??
          reconciliationJobs[0] ??
          null;
        if (selectedJob === null) {
          setReconciliationJob(null);
          setJobErrorMessage(null);
          return null;
        }

        const detail = await readJobDetail(entityId, selectedJob.id);
        setReconciliationJob(detail);
        setJobErrorMessage(null);
        return detail;
      } catch (error: unknown) {
        setJobErrorMessage(resolveJobErrorMessage(error));
        return null;
      }
    },
    [closeRunId, entityId],
  );

  useEffect(() => {
    void refreshReconciliationJob();
  }, [refreshReconciliationJob]);

  useEffect(() => {
    if (reconciliationJob === null || !isActiveJobStatus(reconciliationJob.status)) {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      void refreshReconciliationJob(reconciliationJob.id);
    }, 2000);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [reconciliationJob, refreshReconciliationJob]);

  useEffect(() => {
    if (reconciliationJob === null || !isTerminalJobStatus(reconciliationJob.status)) {
      return;
    }

    const terminalKey = `${reconciliationJob.id}:${reconciliationJob.status}:${reconciliationJob.updated_at}`;
    if (handledTerminalJobKeyRef.current === terminalKey) {
      return;
    }

    handledTerminalJobKeyRef.current = terminalKey;
    void refreshWorkspace();
  }, [reconciliationJob, refreshWorkspace]);

  const reviewWorkspace = workspaceData?.reviewWorkspace ?? null;
  const visibleItems = useMemo(
    () =>
      reviewWorkspace === null
        ? []
        : filterReconciliationItems(reviewWorkspace.items, activeFilter),
    [activeFilter, reviewWorkspace],
  );

  const selectedItem = reviewWorkspace?.items.find((item) => item.id === selectedItemId) ?? null;

  const selectedMatch = selectedItem?.matchedTo[0] ?? null;
  const unresolvedAnomalies =
    reviewWorkspace?.anomalies.filter((anomaly) => !anomaly.resolved) ?? [];
  const actionableAnomalies = unresolvedAnomalies.filter((anomaly) => anomaly.severity !== "info");
  const blockerCount =
    actionableAnomalies.length + (reviewWorkspace?.queueCounts.pendingRunApprovals ?? 0);
  const reviewNextItemId = useMemo(() => selectInitialItemId(reviewWorkspace), [reviewWorkspace]);
  const runStatusLabel = useMemo(() => {
    if (reconciliationJob !== null) {
      return formatLabel(reconciliationJob.status);
    }
    if (reviewWorkspace === null) {
      return "Unavailable";
    }
    if (reviewWorkspace.reconciliations.length === 0) {
      return "Not Run";
    }
    return reviewWorkspace.queueCounts.pendingRunApprovals > 0 ? "Pending Review" : "Active";
  }, [reconciliationJob, reviewWorkspace]);

  const handleRefreshWorkspace = useCallback(async (): Promise<void> => {
    setIsRefreshing(true);
    try {
      await Promise.all([
        refreshWorkspace(),
        refreshReconciliationJob(reconciliationJob?.id ?? queuedRun?.job_id ?? null),
      ]);
    } finally {
      setIsRefreshing(false);
    }
  }, [queuedRun?.job_id, reconciliationJob?.id, refreshReconciliationJob, refreshWorkspace]);

  const handleOpenEvidence = useCallback(
    (itemId: string): void => {
      if (reviewWorkspace === null) {
        return;
      }

      const item = reviewWorkspace.items.find((candidate) => candidate.id === itemId);
      if (item === undefined) {
        return;
      }

      setSelectedItemId(item.id);
      setEvidenceDrawer({
        isOpen: true,
        references: buildEvidenceRefsFromItem(item),
        sourceLabel: item.sourceRef,
        title: "Reconciliation evidence",
      });
    },
    [reviewWorkspace],
  );

  const handleQuickDisposition = useCallback(
    async (
      action: "accepted_as_is" | "adjusted" | "escalated" | "pending_info" | "resolved",
    ): Promise<void> => {
      if (selectedItem === null) {
        return;
      }

      setActiveActionKey(`${action}:${selectedItem.id}`);
      setStatusMessage(null);
      try {
        await submitDispositionItem(
          entityId,
          closeRunId,
          selectedItem.id,
          action,
          selectedItemNote.trim().length > 0
            ? selectedItemNote.trim()
            : buildDefaultDispositionReason(action, selectedItem),
        );
        setSelectedItemNote("");
        setStatusMessage("Disposition saved and reconciliation state refreshed.");
        await refreshWorkspace();
      } catch (error: unknown) {
        setErrorMessage(resolveReconciliationErrorMessage(error));
      } finally {
        setActiveActionKey(null);
      }
    },
    [closeRunId, entityId, refreshWorkspace, selectedItem, selectedItemNote],
  );

  const handleResolveAnomaly = useCallback(
    async (anomalyId: string): Promise<void> => {
      const note = resolutionNotes[anomalyId]?.trim();
      if (!note) {
        return;
      }

      setActiveActionKey(`anomaly:${anomalyId}`);
      try {
        await resolveAnomaly(entityId, closeRunId, anomalyId, note);
        setResolutionNotes((current) => ({ ...current, [anomalyId]: "" }));
        setStatusMessage("Anomaly resolution recorded.");
        await refreshWorkspace();
      } catch (error: unknown) {
        setErrorMessage(resolveReconciliationErrorMessage(error));
      } finally {
        setActiveActionKey(null);
      }
    },
    [closeRunId, entityId, refreshWorkspace, resolutionNotes],
  );

  const handleRunReconciliation = useCallback(async (): Promise<void> => {
    setIsQueueingRun(true);
    setStatusMessage(null);
    try {
      const result = await runReconciliation(entityId, closeRunId);
      setQueuedRun(result);
      handledTerminalJobKeyRef.current = null;
      if (result.job_id !== null) {
        await refreshReconciliationJob(result.job_id);
      }
      setStatusMessage(
        result.message ??
          (result.status === "not_applicable"
            ? "No applicable reconciliation work was detected for this period."
            : "Reconciliation worker queued."),
      );
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveReconciliationErrorMessage(error));
    } finally {
      setIsQueueingRun(false);
    }
  }, [closeRunId, entityId, refreshReconciliationJob, refreshWorkspace]);

  const handleApproveRun = useCallback(
    async (reconciliationId: string): Promise<void> => {
      setActiveActionKey(`approve-run:${reconciliationId}`);
      setStatusMessage(null);
      try {
        await approveReconciliation(
          entityId,
          closeRunId,
          reconciliationId,
          "Approved in reconciliation workspace",
        );
        setStatusMessage("Reconciliation run approved.");
        await refreshWorkspace();
      } catch (error: unknown) {
        setErrorMessage(resolveReconciliationErrorMessage(error));
      } finally {
        setActiveActionKey(null);
      }
    },
    [closeRunId, entityId, refreshWorkspace],
  );

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading reconciliation workspace...</div>
        </section>
      </div>
    );
  }

  if (workspaceData === null || reviewWorkspace === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The reconciliation workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  const closeRun = workspaceData.closeRunWorkspace.closeRun;
  const entityName = workspaceData.closeRunWorkspace.entity.name;
  const varianceAmount = deriveTrialBalanceVariance(reviewWorkspace);
  const primaryHeaderAction =
    reviewWorkspace.reconciliations.length === 0 && reconciliationJob === null ? "run" : "review";
  const approvalDisposition =
    selectedItem === null ? "resolved" : resolveApprovalDisposition(selectedItem);

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <p className="quartz-kpi-label">
              {entityName} • {formatCloseRunPeriod(closeRun)}
            </p>
            <h1>Reconciliation</h1>
            <div className="quartz-header-stat-row">
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Exceptions</span>
                <span className="quartz-header-stat-value error">
                  {reviewWorkspace.queueCounts.exception + reviewWorkspace.queueCounts.unmatched}
                </span>
              </div>
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Blockers</span>
                <span className="quartz-header-stat-value warning">{blockerCount}</span>
              </div>
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Run status</span>
                <span className="quartz-header-stat-value">{runStatusLabel}</span>
              </div>
            </div>
          </div>

          <div className="quartz-page-toolbar">
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/chat`}
            >
              <QuartzIcon className="quartz-inline-icon" name="assistant" />
              Open Assistant
            </Link>
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/schedules`}
            >
              Supporting Schedules
            </Link>
            <button
              className="secondary-button"
              disabled={isRefreshing}
              onClick={() => {
                void handleRefreshWorkspace();
              }}
              type="button"
            >
              {isRefreshing ? "Refreshing..." : "Refresh Workspace"}
            </button>
            <button
              className="primary-button"
              disabled={
                primaryHeaderAction === "review"
                  ? reviewNextItemId === null
                  : isQueueingRun ||
                    (reconciliationJob !== null && isActiveJobStatus(reconciliationJob.status))
              }
              onClick={() => {
                if (primaryHeaderAction === "review") {
                  if (reviewNextItemId !== null) {
                    setSelectedItemId(reviewNextItemId);
                  }
                  return;
                }

                void handleRunReconciliation();
              }}
              type="button"
            >
              {primaryHeaderAction === "review"
                ? "Review Next Exception"
                : isQueueingRun
                  ? "Queueing..."
                  : "Run Reconciliation"}
            </button>
          </div>
        </header>

        {statusMessage ? (
          <div className="status-banner success quartz-section" role="status">
            {statusMessage}
          </div>
        ) : null}

        {jobErrorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {jobErrorMessage}
          </div>
        ) : null}

        {errorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section quartz-reconciliation-layout">
          <div className="quartz-review-main-stack">
            <article className="quartz-selected-review-shell">
              <div className="quartz-selected-review-header">
                <div>
                  <h2 className="quartz-section-title">
                    {selectedItem
                      ? `Match Review: ${truncate(selectedItem.sourceRef, 24)}`
                      : "Match Review"}
                  </h2>
                  <p className="quartz-table-secondary">
                    {selectedItem
                      ? `${formatSourceType(selectedItem.sourceType)} • ${formatMatchStatusLabel(selectedItem.matchStatus)}`
                      : "Select a reconciliation item from the queue."}
                  </p>
                </div>

                <div className="quartz-inline-action-row">
                  <button
                    className="secondary-button"
                    disabled={
                      selectedItem === null ||
                      !selectedItem.requiresDisposition ||
                      selectedItem.disposition !== null ||
                      activeActionKey !== null
                    }
                    onClick={() => {
                      void handleQuickDisposition("escalated");
                    }}
                    type="button"
                  >
                    {selectedItem !== null && activeActionKey === `escalated:${selectedItem.id}`
                      ? "Saving..."
                      : "Flag for Review"}
                  </button>
                  <button
                    className="primary-button"
                    disabled={
                      selectedItem === null ||
                      !selectedItem.requiresDisposition ||
                      selectedItem.disposition !== null ||
                      activeActionKey !== null
                    }
                    onClick={() => {
                      if (selectedItem !== null) {
                        void handleQuickDisposition(approvalDisposition);
                      }
                    }}
                    type="button"
                  >
                    {selectedItem !== null &&
                    activeActionKey === `${approvalDisposition}:${selectedItem.id}`
                      ? "Saving..."
                      : "Approve Match"}
                  </button>
                </div>
              </div>

              {selectedItem === null ? (
                <div className="quartz-empty-state">
                  Select a queue row to inspect the system record, matched line, and disposition
                  controls.
                </div>
              ) : (
                <>
                  <div className="quartz-comparison-grid">
                    <div className="quartz-comparison-pane">
                      <div>
                        <p className="quartz-kpi-label">System Record</p>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Reference</span>
                        <strong>{selectedItem.sourceRef}</strong>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Date</span>
                        <strong>
                          {selectedItem.periodDate
                            ? formatDate(selectedItem.periodDate)
                            : "Unknown"}
                        </strong>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Amount</span>
                        <strong>{formatAmount(selectedItem.amount)}</strong>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Difference</span>
                        <strong
                          className={
                            isNonZeroDifference(selectedItem.differenceAmount) ? "error" : undefined
                          }
                        >
                          {formatAmount(selectedItem.differenceAmount)}
                        </strong>
                      </div>
                    </div>

                    <div className="quartz-comparison-pane">
                      <div>
                        <p className="quartz-kpi-label">Statement Line</p>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Reference</span>
                        <strong>{selectedMatch?.sourceRef ?? "No match candidate"}</strong>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Type</span>
                        <strong>
                          {selectedMatch
                            ? formatSourceType(selectedMatch.sourceType)
                            : "Unavailable"}
                        </strong>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Amount</span>
                        <strong
                          className={
                            isNonZeroDifference(selectedItem.differenceAmount) ? "error" : undefined
                          }
                        >
                          {selectedMatch?.amount ? formatAmount(selectedMatch.amount) : "Unknown"}
                        </strong>
                      </div>
                      <div className="quartz-comparison-row">
                        <span className="quartz-table-secondary">Confidence</span>
                        <strong>
                          {selectedMatch?.confidence !== null &&
                          selectedMatch?.confidence !== undefined
                            ? `${Math.round(selectedMatch.confidence * 100)}%`
                            : "Rule-based"}
                        </strong>
                      </div>
                    </div>
                  </div>

                  <div className="quartz-card-form-area">
                    {selectedItem.explanation ? (
                      <div className="quartz-highlight-box">
                        <span className="quartz-table-secondary">Explanation</span>
                        <p className="form-helper">{selectedItem.explanation}</p>
                      </div>
                    ) : null}

                    <label>
                      <span className="quartz-kpi-label">Disposition note</span>
                      <textarea
                        className="text-input"
                        onChange={(event) => setSelectedItemNote(event.target.value)}
                        placeholder="Optional reviewer note. If blank, the workspace records a default operational reason."
                        value={selectedItemNote}
                      />
                    </label>

                    <div className="quartz-inline-action-row">
                      <button
                        className="secondary-button"
                        onClick={() => handleOpenEvidence(selectedItem.id)}
                        type="button"
                      >
                        Open Evidence
                      </button>
                      <button
                        className="secondary-button"
                        disabled={
                          !selectedItem.requiresDisposition ||
                          selectedItem.disposition !== null ||
                          activeActionKey !== null
                        }
                        onClick={() => {
                          void handleQuickDisposition("adjusted");
                        }}
                        type="button"
                      >
                        {activeActionKey === `adjusted:${selectedItem.id}`
                          ? "Saving..."
                          : "Needs Adjustment"}
                      </button>
                      <button
                        className="secondary-button"
                        disabled={
                          !selectedItem.requiresDisposition ||
                          selectedItem.disposition !== null ||
                          activeActionKey !== null
                        }
                        onClick={() => {
                          void handleQuickDisposition("pending_info");
                        }}
                        type="button"
                      >
                        {activeActionKey === `pending_info:${selectedItem.id}`
                          ? "Saving..."
                          : "Request Info"}
                      </button>
                    </div>

                    {selectedItem.disposition ? (
                      <div className="status-banner info" role="status">
                        Disposition recorded: {formatLabel(selectedItem.disposition)}.
                        {selectedItem.dispositionReason ? ` ${selectedItem.dispositionReason}` : ""}
                      </div>
                    ) : null}
                  </div>
                </>
              )}
            </article>

            <article className="quartz-card quartz-card-table-shell">
              <div className="quartz-section-header">
                <h2 className="quartz-section-title">Reconciliation Queue</h2>
                <span className="quartz-queue-meta">
                  Showing {visibleItems.length} of {reviewWorkspace.items.length}
                </span>
              </div>

              <div className="quartz-filter-chip-row" style={{ padding: "0 16px 16px" }}>
                {reconciliationFilters.map((filter) => (
                  <button
                    className={
                      activeFilter === filter.filter
                        ? "quartz-filter-chip active"
                        : "quartz-filter-chip"
                    }
                    key={filter.filter}
                    onClick={() => setActiveFilter(filter.filter)}
                    type="button"
                  >
                    {filter.label}
                  </button>
                ))}
              </div>

              <table className="quartz-table">
                <thead>
                  <tr>
                    <th>Sev</th>
                    <th>Type</th>
                    <th>Amount (NGN)</th>
                    <th>Difference</th>
                    <th>Status</th>
                    <th>Disposition</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleItems.length === 0 ? (
                    <tr>
                      <td colSpan={6}>
                        <div className="quartz-empty-state">
                          No reconciliation items match the selected filter.
                        </div>
                      </td>
                    </tr>
                  ) : (
                    visibleItems.map((item) => (
                      <tr
                        className={
                          selectedItem?.id === item.id
                            ? `quartz-table-row selected ${
                                item.matchStatus === "exception" ? "error" : ""
                              }`.trim()
                            : item.matchStatus === "exception"
                              ? "quartz-table-row error"
                              : ""
                        }
                        key={item.id}
                        onClick={() => setSelectedItemId(item.id)}
                      >
                        <td>
                          <span className={`quartz-compact-pill ${resolveItemSeverityTone(item)}`}>
                            {resolveItemSeverityLabel(item)}
                          </span>
                        </td>
                        <td>
                          <div className="quartz-table-primary">
                            {formatSourceType(item.sourceType)}
                          </div>
                          <div className="quartz-table-secondary">
                            {truncate(item.sourceRef, 28)}
                          </div>
                        </td>
                        <td className="quartz-table-numeric">{formatAmount(item.amount)}</td>
                        <td className="quartz-table-numeric">
                          {formatAmount(item.differenceAmount)}
                        </td>
                        <td>
                          <span
                            className={`quartz-status-badge ${resolveMatchStatusTone(item.matchStatus)}`}
                          >
                            {formatMatchStatusLabel(item.matchStatus)}
                          </span>
                        </td>
                        <td>
                          {item.disposition
                            ? formatLabel(item.disposition)
                            : item.requiresDisposition
                              ? "Pending"
                              : "N/A"}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </article>
          </div>

          <div className="quartz-reconciliation-support-stack">
            <article className="quartz-card">
              <p className="quartz-card-eyebrow">Period Summary</p>
              <div className="quartz-summary-list">
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Total Debits</span>
                  <strong>{reviewWorkspace.trialBalance?.totalDebits ?? "—"}</strong>
                </div>
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Total Credits</span>
                  <strong>{reviewWorkspace.trialBalance?.totalCredits ?? "—"}</strong>
                </div>
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Variance</span>
                  <strong
                    className={
                      varianceAmount !== null && varianceAmount !== "0.00" ? "error" : undefined
                    }
                  >
                    {varianceAmount ?? "—"}
                  </strong>
                </div>
              </div>

              <div className="quartz-divider quartz-section" />

              <div className="quartz-mini-list">
                {reviewWorkspace.reconciliations.length === 0 ? (
                  <div className="quartz-mini-item">
                    <strong>No reconciliation runs yet</strong>
                    <span className="quartz-mini-meta">
                      Run the engine to populate bank-match and trial-balance review data.
                    </span>
                  </div>
                ) : (
                  reviewWorkspace.reconciliations.map((reconciliation) => (
                    <div className="quartz-mini-item" key={reconciliation.id}>
                      <strong>
                        {formatReconciliationTypeLabel(reconciliation.reconciliationType)}
                      </strong>
                      <span className="quartz-mini-meta">
                        {formatLabel(reconciliation.status)} • {reconciliation.itemCount} item(s)
                      </span>
                      <button
                        className="secondary-button"
                        disabled={reconciliation.status === "approved" || activeActionKey !== null}
                        onClick={() => {
                          void handleApproveRun(reconciliation.id);
                        }}
                        type="button"
                      >
                        {activeActionKey === `approve-run:${reconciliation.id}`
                          ? "Saving..."
                          : reconciliation.status === "approved"
                            ? "Approved"
                            : "Approve Run"}
                      </button>
                    </div>
                  ))
                )}
              </div>
            </article>

            <article className="quartz-card">
              <p className="quartz-card-eyebrow error">Anomalies</p>
              <div className="quartz-anomaly-stack">
                {unresolvedAnomalies.length === 0 ? (
                  <div className="quartz-mini-item">
                    <strong>No unresolved anomalies</strong>
                    <span className="quartz-mini-meta">
                      This workspace is clear on trial-balance and control findings.
                    </span>
                  </div>
                ) : (
                  unresolvedAnomalies.slice(0, 3).map((anomaly) => (
                    <div
                      className={`quartz-anomaly-card ${resolveAnomalyTone(anomaly.severity)}`}
                      key={anomaly.id}
                    >
                      <strong>
                        {anomaly.accountCode
                          ? `Account ${anomaly.accountCode}`
                          : formatLabel(anomaly.anomalyType)}
                      </strong>
                      <span className="quartz-mini-meta">{anomaly.description}</span>
                      <textarea
                        className="text-input"
                        onChange={(event) =>
                          setResolutionNotes((current) => ({
                            ...current,
                            [anomaly.id]: event.target.value,
                          }))
                        }
                        placeholder="Resolution note..."
                        value={resolutionNotes[anomaly.id] ?? ""}
                      />
                      <button
                        className="secondary-button"
                        disabled={
                          activeActionKey !== null ||
                          (resolutionNotes[anomaly.id] ?? "").trim().length === 0
                        }
                        onClick={() => {
                          void handleResolveAnomaly(anomaly.id);
                        }}
                        type="button"
                      >
                        {activeActionKey === `anomaly:${anomaly.id}` ? "Saving..." : "Resolve"}
                      </button>
                    </div>
                  ))
                )}
              </div>
            </article>

            <article className="quartz-card">
              <p className="quartz-card-eyebrow">Evidence</p>
              <EvidenceDrawer
                emptyMessage="Select a queue row and open evidence to inspect source-backed references."
                isOpen={evidenceDrawer.isOpen}
                onClose={() => setEvidenceDrawer(defaultEvidenceDrawerState)}
                references={evidenceDrawer.references}
                sourceLabel={evidenceDrawer.sourceLabel}
                title={evidenceDrawer.title}
              />
            </article>
            <article className="quartz-card ai">
              <p className="quartz-card-eyebrow secondary">Variance analysis</p>
              <h3>
                {selectedItem
                  ? `Difference ${formatAmount(selectedItem.differenceAmount)}`
                  : "Select an exception"}
              </h3>
              <p className="form-helper">
                {selectedItem?.explanation ??
                  actionableAnomalies[0]?.description ??
                  "Select a queue row to inspect the current mismatch, supporting records, and next operational step."}
              </p>
            </article>

            <article className="quartz-card">
              <p className="quartz-card-eyebrow">Confidence</p>
              <div className="quartz-summary-list">
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Match quality</span>
                  <strong>
                    {selectedMatch?.confidence !== null && selectedMatch?.confidence !== undefined
                      ? `${Math.round(selectedMatch.confidence * 100)}%`
                      : "Rule-based"}
                  </strong>
                </div>
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Next action</span>
                  <strong>
                    {reviewWorkspace.queueCounts.needsDecision > 0
                      ? "Clear queue"
                      : reviewWorkspace.queueCounts.pendingRunApprovals > 0
                        ? "Approve runs"
                        : "Advance to reporting"}
                  </strong>
                </div>
              </div>
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}

function selectInitialItemId(workspace: ReconciliationReviewWorkspaceData | null): string | null {
  if (workspace === null) {
    return null;
  }

  return (
    workspace.items.find((item) => item.requiresDisposition && item.disposition === null)?.id ??
    workspace.items.find((item) => item.matchStatus === "exception")?.id ??
    workspace.items[0]?.id ??
    null
  );
}

function buildEvidenceRefsFromItem(item: {
  matchedTo: ReadonlyArray<{ confidence: number | null; sourceRef: string; sourceType: string }>;
  sourceRef: string;
}): EvidenceDrawerReference[] {
  const refs: EvidenceDrawerReference[] = [
    {
      id: `source-${item.sourceRef}`,
      kind: "source",
      label: "Source Record",
      location: item.sourceRef,
      snippet: `Source: ${item.sourceRef}`,
    },
  ];

  for (const counterpart of item.matchedTo) {
    refs.push({
      confidence: counterpart.confidence ?? null,
      id: `match-${counterpart.sourceRef}`,
      kind: "match",
      label: `Match: ${counterpart.sourceType}`,
      location: counterpart.sourceRef,
      snippet: `Matched to ${counterpart.sourceRef}`,
    });
  }

  return refs;
}

function buildDefaultDispositionReason(
  action: "accepted_as_is" | "adjusted" | "escalated" | "pending_info" | "resolved",
  item: ReconciliationItemSummary,
): string {
  switch (action) {
    case "accepted_as_is":
      return `Accepted as-is for ${item.sourceRef} from the reconciliation workspace.`;
    case "adjusted":
      return `Flagged ${item.sourceRef} for adjustment from the reconciliation workspace.`;
    case "escalated":
      return `Escalated ${item.sourceRef} for further review from the reconciliation workspace.`;
    case "pending_info":
      return `Additional information requested for ${item.sourceRef} from the reconciliation workspace.`;
    case "resolved":
      return `Resolved ${item.sourceRef} in the reconciliation workspace.`;
  }
}

function resolveApprovalDisposition(
  item: ReconciliationItemSummary,
): "accepted_as_is" | "resolved" {
  if (item.matchStatus === "matched" || item.matchStatus === "partially_matched") {
    return "accepted_as_is";
  }

  return "resolved";
}

function deriveTrialBalanceVariance(workspace: ReconciliationReviewWorkspaceData): string | null {
  if (workspace.trialBalance === null) {
    return null;
  }

  const debitAmount = parseNumber(workspace.trialBalance.totalDebits);
  const creditAmount = parseNumber(workspace.trialBalance.totalCredits);
  if (debitAmount === null || creditAmount === null) {
    return null;
  }

  const variance = debitAmount - creditAmount;
  return variance.toLocaleString("en-NG", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function parseNumber(value: string): number | null {
  const normalized = value.replaceAll(",", "").replace(/[^\d.-]/gu, "");
  if (normalized.trim().length === 0) {
    return null;
  }

  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function resolveItemSeverityLabel(item: ReconciliationItemSummary): string {
  if (item.matchStatus === "exception") {
    return "High";
  }
  if (item.matchStatus === "unmatched") {
    return "Review";
  }
  return "Stable";
}

function resolveItemSeverityTone(item: ReconciliationItemSummary): "error" | "success" | "warning" {
  if (item.matchStatus === "exception") {
    return "error";
  }
  if (item.matchStatus === "unmatched") {
    return "warning";
  }
  return "success";
}

function resolveMatchStatusTone(status: string): "error" | "neutral" | "success" | "warning" {
  if (status === "exception") {
    return "error";
  }
  if (status === "unmatched") {
    return "warning";
  }
  if (status === "matched" || status === "partially_matched") {
    return "success";
  }
  return "neutral";
}

function resolveAnomalyTone(severity: string): "error" | "info" | "warning" {
  if (severity === "blocking") {
    return "error";
  }
  if (severity === "warning") {
    return "warning";
  }
  return "info";
}

function formatSourceType(sourceType: string): string {
  const labels: Readonly<Record<string, string>> = {
    bank_statement_line: "Bank Line",
    external_balance: "External Balance",
    ledger_transaction: "Ledger Transaction",
    manual_adjustment: "Manual Adjustment",
    recommendation: "Recommendation",
  };

  return labels[sourceType] ?? formatLabel(sourceType);
}

function formatAmount(value: string | null): string {
  if (value === null || value === undefined) {
    return "—";
  }

  const parsed = Number(value.replaceAll(",", ""));
  if (!Number.isFinite(parsed)) {
    return value;
  }

  return parsed.toLocaleString("en-NG", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function isNonZeroDifference(value: string | null): boolean {
  if (value === null || value === undefined) {
    return false;
  }

  const parsed = Number(value.replaceAll(",", ""));
  return Number.isFinite(parsed) && Math.abs(parsed) > 0.005;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
  }).format(new Date(value));
}

function formatLabel(value: string): string {
  return value
    .replaceAll("-", "_")
    .split("_")
    .filter((part) => part.length > 0)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) {
    return value;
  }

  return `${value.slice(0, maxLength - 3)}...`;
}

function resolveReconciliationErrorMessage(error: unknown): string {
  if (error instanceof ReconciliationApiError || error instanceof CloseRunApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return "The reconciliation request failed. Retry after refreshing the workspace.";
}

function resolveJobErrorMessage(error: unknown): string {
  if (error instanceof JobApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return "Failed to read the reconciliation worker status.";
}

function isActiveJobStatus(status: string): boolean {
  return status === "queued" || status === "running";
}

function isTerminalJobStatus(status: string): boolean {
  return (
    status === "blocked" || status === "canceled" || status === "completed" || status === "failed"
  );
}
