/*
Purpose: Render the Quartz recommendations and journals workspace for one close run.
Scope: Recommendation generation, accountant review, journal approval/posting, and
       assistant-guided rationale without disconnecting from the real APIs.
Dependencies: Close-run context reads, recommendation/journal API helpers, and
              the shared Quartz assistant rail.
*/

"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import {
  CloseRunApiError,
  formatCloseRunPeriod,
  readCloseRunWorkspace,
  type CloseRunWorkspaceData,
} from "../../../../../../../lib/close-runs";
import {
  approveJournal,
  approveRecommendation,
  applyJournal,
  buildJournalPostingDownloadPath,
  generateRecommendations,
  JOURNAL_POSTING_TARGET_LABELS,
  listJournals,
  listRecommendations,
  RecommendationApiError,
  rejectJournal,
  rejectRecommendation,
  type JournalPostingSummary,
  type JournalSummary,
  type RecommendationSummary,
} from "../../../../../../../lib/recommendations";

type RecommendationsPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

type RecommendationsWorkspaceData = {
  closeRunWorkspace: CloseRunWorkspaceData;
  journals: readonly JournalSummary[];
  recommendations: readonly RecommendationSummary[];
};

const emptyRecommendations: readonly RecommendationSummary[] = [];
const emptyJournals: readonly JournalSummary[] = [];

export default function RecommendationsPage({
  params,
}: Readonly<RecommendationsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);

  const [activeActionKey, setActiveActionKey] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isGenerating, setIsGenerating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [selectedJournalId, setSelectedJournalId] = useState<string | null>(null);
  const [selectedRecommendationId, setSelectedRecommendationId] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<RecommendationsWorkspaceData | null>(null);

  const refreshWorkspace = useCallback(async (): Promise<void> => {
    setIsLoading(true);
    try {
      const [closeRunWorkspace, nextRecommendations, nextJournals] = await Promise.all([
        readCloseRunWorkspace(entityId, closeRunId),
        listRecommendations(entityId, closeRunId),
        listJournals(entityId, closeRunId),
      ]);

      const visibleRecommendations = nextRecommendations.filter(
        (recommendation) => recommendation.status !== "superseded",
      );
      const visibleJournals = nextJournals.filter((journal) => journal.status !== "superseded");

      setWorkspaceData({
        closeRunWorkspace,
        journals: visibleJournals,
        recommendations: visibleRecommendations,
      });

      setSelectedRecommendationId((currentSelectedRecommendationId) => {
        if (
          currentSelectedRecommendationId !== null &&
          visibleRecommendations.some(
            (recommendation) => recommendation.id === currentSelectedRecommendationId,
          )
        ) {
          return currentSelectedRecommendationId;
        }

        return selectNextRecommendationId(visibleRecommendations);
      });

      setSelectedJournalId((currentSelectedJournalId) => {
        if (
          currentSelectedJournalId !== null &&
          visibleJournals.some((journal) => journal.id === currentSelectedJournalId)
        ) {
          return currentSelectedJournalId;
        }

        return visibleJournals[0]?.id ?? null;
      });

      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationsErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId]);

  useEffect(() => {
    void refreshWorkspace();
  }, [refreshWorkspace]);

  const recommendations = workspaceData?.recommendations ?? emptyRecommendations;
  const journals = workspaceData?.journals ?? emptyJournals;

  const journalsByRecommendationId = useMemo(() => {
    const nextMap = new Map<string, JournalSummary>();
    for (const journal of journals) {
      if (journal.recommendation_id !== null && !nextMap.has(journal.recommendation_id)) {
        nextMap.set(journal.recommendation_id, journal);
      }
    }
    return nextMap;
  }, [journals]);

  const selectedRecommendation =
    recommendations.find((recommendation) => recommendation.id === selectedRecommendationId) ??
    null;

  const linkedJournalForSelectedRecommendation =
    selectedRecommendation !== null
      ? (journalsByRecommendationId.get(selectedRecommendation.id) ?? null)
      : null;

  const selectedJournal =
    linkedJournalForSelectedRecommendation ??
    journals.find((journal) => journal.id === selectedJournalId) ??
    null;

  const metrics = useMemo(() => buildRecommendationMetrics(recommendations), [recommendations]);
  const selectedRecommendationAmount = useMemo(
    () => deriveRecommendationAmount(selectedRecommendation, selectedJournal),
    [selectedJournal, selectedRecommendation],
  );
  const nextRecommendationId = useMemo(
    () => selectNextRecommendationId(recommendations),
    [recommendations],
  );

  const handleSelectRecommendation = useCallback(
    (recommendationId: string): void => {
      setSelectedRecommendationId(recommendationId);
      const linkedJournal = journalsByRecommendationId.get(recommendationId) ?? null;
      if (linkedJournal !== null) {
        setSelectedJournalId(linkedJournal.id);
      }
    },
    [journalsByRecommendationId],
  );

  async function handleGenerateRecommendations(force = false): Promise<void> {
    setIsGenerating(true);
    setStatusMessage(null);
    try {
      const result = await generateRecommendations(entityId, closeRunId, { force });
      setStatusMessage(buildQueuedRecommendationMessage(result, force));
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationsErrorMessage(error));
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleRecommendationAction(
    recommendationId: string,
    action: "approve" | "reject",
  ): Promise<void> {
    setActiveActionKey(`${action}:${recommendationId}`);
    setStatusMessage(null);
    try {
      if (action === "approve") {
        await approveRecommendation(
          entityId,
          closeRunId,
          recommendationId,
          "Approved in recommendations workspace",
        );
        setStatusMessage("Recommendation approved. Journal draft state refreshed.");
      } else {
        await rejectRecommendation(
          entityId,
          closeRunId,
          recommendationId,
          "Rejected in recommendations workspace",
        );
        setStatusMessage("Recommendation rejected.");
      }
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationsErrorMessage(error));
    } finally {
      setActiveActionKey(null);
    }
  }

  async function handleJournalAction(
    journalId: string,
    action: "approve" | "apply_external" | "apply_internal" | "reject",
  ): Promise<void> {
    setActiveActionKey(`${action}:${journalId}`);
    setStatusMessage(null);
    try {
      if (action === "approve") {
        await approveJournal(entityId, closeRunId, journalId, "Approved in journal workspace");
        setStatusMessage("Draft journal approved.");
      } else if (action === "apply_internal") {
        await applyJournal(
          entityId,
          closeRunId,
          journalId,
          "internal_ledger",
          "Posted from recommendations workspace",
        );
        setStatusMessage("Journal posted to the internal ledger.");
      } else if (action === "apply_external") {
        await applyJournal(
          entityId,
          closeRunId,
          journalId,
          "external_erp_package",
          "ERP package generated from recommendations workspace",
        );
        setStatusMessage("ERP import package generated.");
      } else {
        await rejectJournal(entityId, closeRunId, journalId, "Rejected in journal workspace");
        setStatusMessage("Draft journal rejected.");
      }
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationsErrorMessage(error));
    } finally {
      setActiveActionKey(null);
    }
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading recommendations and journals...</div>
        </section>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The recommendations workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  const closeRun = workspaceData.closeRunWorkspace.closeRun;
  const entityName = workspaceData.closeRunWorkspace.entity.name;
  const recommendationEvidenceHref = `/entities/${entityId}/close-runs/${closeRunId}/documents`;

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <p className="quartz-kpi-label">
              {entityName} • {formatCloseRunPeriod(closeRun)}
            </p>
            <h1>Recommendations and Journals</h1>
            <p className="quartz-page-subtitle">
              Review AI-proposed accounting treatment and resulting draft journals before the close
              advances into reconciliation.
            </p>

            <div className="quartz-header-stat-row">
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Coverage</span>
                <span className="quartz-header-stat-value">{metrics.coverage}%</span>
              </div>
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Pending</span>
                <span className="quartz-header-stat-value warning">{metrics.pendingCount}</span>
              </div>
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Approved</span>
                <span className="quartz-header-stat-value success">{metrics.approvedCount}</span>
              </div>
            </div>
          </div>

          <div className="quartz-page-toolbar">
            <button
              className="secondary-button"
              disabled={isGenerating}
              onClick={() => {
                void handleGenerateRecommendations(false);
              }}
              type="button"
            >
              {isGenerating ? "Queueing..." : "Generate Recommendations"}
            </button>
            <button
              className="secondary-button"
              disabled={isGenerating || recommendations.length === 0}
              onClick={() => {
                void handleGenerateRecommendations(true);
              }}
              type="button"
            >
              {isGenerating ? "Queueing..." : "Regenerate Recommendations"}
            </button>
            <button
              className="primary-button"
              disabled={nextRecommendationId === null}
              onClick={() => {
                if (nextRecommendationId !== null) {
                  handleSelectRecommendation(nextRecommendationId);
                }
              }}
              type="button"
            >
              Review Next Recommendation
            </button>
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

        <section className="quartz-section quartz-review-layout">
          <div className="quartz-review-main-stack">
            <article className="quartz-card quartz-card-table-shell">
              <div className="quartz-section-header">
                <h2 className="quartz-section-title">Queue</h2>
                <span className="quartz-queue-meta">
                  {recommendations.length} recommendation{recommendations.length === 1 ? "" : "s"}
                </span>
              </div>

              <table className="quartz-table">
                <thead>
                  <tr>
                    <th>Confidence</th>
                    <th>Source Document</th>
                    <th>Amount (NGN)</th>
                    <th>Proposed Treatment</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {recommendations.length === 0 ? (
                    <tr>
                      <td colSpan={5}>
                        <div className="quartz-empty-state">
                          No recommendation rows exist yet. Generate recommendations to review the
                          current document set.
                        </div>
                      </td>
                    </tr>
                  ) : (
                    recommendations.map((recommendation) => {
                      const linkedJournal =
                        journalsByRecommendationId.get(recommendation.id) ?? null;
                      const confidenceTone = resolveConfidenceTone(recommendation.confidence);
                      const isSelected = selectedRecommendation?.id === recommendation.id;
                      const statusTone = resolveRecommendationStatusTone(recommendation.status);

                      return (
                        <tr
                          className={
                            isSelected
                              ? `quartz-table-row selected ${statusTone === "error" ? "error" : ""}`.trim()
                              : statusTone === "error"
                                ? "quartz-table-row error"
                                : ""
                          }
                          key={recommendation.id}
                          onClick={() => handleSelectRecommendation(recommendation.id)}
                        >
                          <td>
                            <span className={`quartz-compact-pill ${confidenceTone}`}>
                              {formatConfidenceLabel(recommendation.confidence)}
                            </span>
                          </td>
                          <td>
                            <div className="quartz-table-primary">
                              {recommendation.source_document_filename ?? "Source document pending"}
                            </div>
                            <div className="quartz-table-secondary">
                              {recommendation.source_document_type
                                ? formatLabel(recommendation.source_document_type)
                                : "Document metadata unavailable"}
                            </div>
                          </td>
                          <td className="quartz-table-numeric">
                            {deriveRecommendationAmount(recommendation, linkedJournal)}
                          </td>
                          <td>{formatLabel(recommendation.recommendation_type)}</td>
                          <td>
                            <span className={`quartz-status-badge ${statusTone}`}>
                              {formatLabel(recommendation.status)}
                            </span>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </article>

            <article className="quartz-card quartz-card-table-shell">
              <div className="quartz-section-header">
                <div>
                  <h2 className="quartz-section-title">Draft Journal Entry</h2>
                  <p className="quartz-table-secondary">
                    {selectedJournal?.journal_number ??
                      "Approve a recommendation to create a draft"}
                  </p>
                </div>
                <span className="quartz-compact-pill warning">AI Proposed</span>
              </div>

              {selectedJournal === null ? (
                <div className="quartz-empty-state">
                  Select a reviewed recommendation to inspect the linked journal draft.
                </div>
              ) : (
                <div className="quartz-review-card-stack">
                  <div className="quartz-card">
                    <div className="quartz-form-grid">
                      <label>
                        <span className="quartz-kpi-label">Narration</span>
                        <input
                          className="text-input"
                          readOnly
                          type="text"
                          value={selectedJournal.description}
                        />
                      </label>
                      <label>
                        <span className="quartz-kpi-label">Date</span>
                        <input
                          className="text-input"
                          readOnly
                          type="text"
                          value={selectedJournal.posting_date}
                        />
                      </label>
                    </div>
                  </div>

                  {journals.length > 1 ? (
                    <div className="quartz-card">
                      <p className="quartz-kpi-label">Available Drafts</p>
                      <div className="quartz-journal-selector">
                        {journals.map((journal) => (
                          <button
                            className={
                              selectedJournal.id === journal.id
                                ? "quartz-filter-chip active"
                                : "quartz-filter-chip"
                            }
                            key={journal.id}
                            onClick={() => setSelectedJournalId(journal.id)}
                            type="button"
                          >
                            {journal.journal_number}
                          </button>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  <table className="quartz-table quartz-journal-lines">
                    <thead>
                      <tr>
                        <th>Account</th>
                        <th>Dimensions</th>
                        <th>Debit</th>
                        <th>Credit</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedJournal.lines.map((line) => (
                        <tr key={line.id}>
                          <td>
                            <div className="quartz-table-primary">{line.account_code}</div>
                            <div className="quartz-table-secondary">
                              {line.description ?? `${formatLabel(line.line_type)} line`}
                            </div>
                          </td>
                          <td>
                            {Object.keys(line.dimensions).length === 0 ? (
                              <span className="quartz-table-secondary">No dimensions</span>
                            ) : (
                              <div className="quartz-table-secondary">
                                {Object.entries(line.dimensions)
                                  .map(
                                    ([dimensionKey, dimensionValue]) =>
                                      `${formatLabel(dimensionKey)}: ${dimensionValue}`,
                                  )
                                  .join(" • ")}
                              </div>
                            )}
                          </td>
                          <td className="quartz-table-numeric">
                            {line.line_type.toLowerCase().includes("debit") ? line.amount : "—"}
                          </td>
                          <td className="quartz-table-numeric">
                            {line.line_type.toLowerCase().includes("credit") ? line.amount : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr>
                        <td colSpan={2}>Totals</td>
                        <td className="quartz-table-numeric">{selectedJournal.total_debits}</td>
                        <td className="quartz-table-numeric">{selectedJournal.total_credits}</td>
                      </tr>
                    </tfoot>
                  </table>

                  <div className="quartz-card">
                    <div className="quartz-inline-action-row">
                      <button
                        className="secondary-button"
                        disabled={
                          activeActionKey !== null ||
                          (selectedJournal.status !== "draft" &&
                            selectedJournal.status !== "pending_review")
                        }
                        onClick={() => {
                          void handleJournalAction(selectedJournal.id, "approve");
                        }}
                        type="button"
                      >
                        {activeActionKey === `approve:${selectedJournal.id}`
                          ? "Saving..."
                          : "Approve Journal"}
                      </button>
                      <button
                        className="secondary-button"
                        disabled={
                          activeActionKey !== null ||
                          selectedJournal.status !== "approved" ||
                          selectedJournal.postings.length > 0
                        }
                        onClick={() => {
                          void handleJournalAction(selectedJournal.id, "apply_internal");
                        }}
                        type="button"
                      >
                        {activeActionKey === `apply_internal:${selectedJournal.id}`
                          ? "Saving..."
                          : "Post Internally"}
                      </button>
                      <button
                        className="secondary-button"
                        disabled={
                          activeActionKey !== null ||
                          selectedJournal.status !== "approved" ||
                          selectedJournal.postings.length > 0
                        }
                        onClick={() => {
                          void handleJournalAction(selectedJournal.id, "apply_external");
                        }}
                        type="button"
                      >
                        {activeActionKey === `apply_external:${selectedJournal.id}`
                          ? "Saving..."
                          : "Create ERP CSV"}
                      </button>
                      <button
                        className="secondary-button"
                        disabled={
                          activeActionKey !== null ||
                          selectedJournal.status === "applied" ||
                          selectedJournal.postings.length > 0
                        }
                        onClick={() => {
                          void handleJournalAction(selectedJournal.id, "reject");
                        }}
                        type="button"
                      >
                        {activeActionKey === `reject:${selectedJournal.id}`
                          ? "Saving..."
                          : "Reject Journal"}
                      </button>
                    </div>

                    {selectedJournal.postings.length > 0 ? (
                      <div className="quartz-mini-list">
                        {selectedJournal.postings.map((posting) => (
                          <div className="quartz-mini-item" key={posting.id}>
                            <strong>{formatPostingLabel(posting)}</strong>
                            <span className="quartz-mini-meta">
                              {formatLabel(posting.status)} •{" "}
                              {new Date(posting.posted_at).toLocaleString()}
                            </span>
                            {posting.artifact_filename ? (
                              <a
                                className="quartz-action-link"
                                href={buildJournalPostingDownloadPath(
                                  entityId,
                                  closeRunId,
                                  selectedJournal.id,
                                  posting.id,
                                )}
                              >
                                Download {posting.artifact_filename}
                              </a>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </div>
              )}
            </article>
          </div>

          <div className="quartz-review-support-stack">
            <article className="quartz-card">
              <p className="quartz-card-eyebrow">Source Evidence</p>
              <h3>{selectedRecommendation?.source_document_filename ?? "No document selected"}</h3>
              <div className="quartz-evidence-preview">
                <div className="quartz-evidence-ghost">Source</div>
                <div className="quartz-summary-list">
                  <div className="quartz-summary-row">
                    <span className="quartz-table-secondary">Document type</span>
                    <strong>
                      {selectedRecommendation?.source_document_type
                        ? formatLabel(selectedRecommendation.source_document_type)
                        : "Pending"}
                    </strong>
                  </div>
                  <div className="quartz-summary-row">
                    <span className="quartz-table-secondary">Linked amount</span>
                    <strong>{selectedRecommendationAmount}</strong>
                  </div>
                  <div className="quartz-summary-row">
                    <span className="quartz-table-secondary">Queue status</span>
                    <strong>
                      {selectedRecommendation
                        ? formatLabel(selectedRecommendation.status)
                        : "No selection"}
                    </strong>
                  </div>
                </div>
              </div>
              <div className="quartz-inline-action-row">
                <Link className="secondary-button" href={recommendationEvidenceHref}>
                  Open Inputs Workspace
                </Link>
              </div>
            </article>

            <article className="quartz-card">
              <p className="quartz-card-eyebrow secondary">Review Actions</p>
              <h3>
                {selectedRecommendation?.recommendation_type
                  ? formatLabel(selectedRecommendation.recommendation_type)
                  : "Select a recommendation"}
              </h3>
              <p className="form-helper">
                {selectedRecommendation?.reasoning_summary ??
                  "Select a queue row to inspect the proposed treatment, reasoning, and linked journal draft."}
              </p>
              {selectedRecommendation ? (
                <div className="quartz-inline-action-row">
                  <button
                    className="primary-button"
                    disabled={activeActionKey !== null}
                    onClick={() => {
                      void handleRecommendationAction(selectedRecommendation.id, "approve");
                    }}
                    type="button"
                  >
                    {activeActionKey === `approve:${selectedRecommendation.id}`
                      ? "Saving..."
                      : "Approve Recommendation"}
                  </button>
                  <button
                    className="secondary-button"
                    disabled={activeActionKey !== null}
                    onClick={() => {
                      void handleRecommendationAction(selectedRecommendation.id, "reject");
                    }}
                    type="button"
                  >
                    {activeActionKey === `reject:${selectedRecommendation.id}`
                      ? "Saving..."
                      : "Reject Recommendation"}
                  </button>
                </div>
              ) : null}
            </article>
            <article className="quartz-card ai">
              <p className="quartz-card-eyebrow secondary">Treatment rationale</p>
              <h3>
                {selectedRecommendation
                  ? formatLabel(selectedRecommendation.recommendation_type)
                  : "Awaiting recommendation selection"}
              </h3>
              <p className="form-helper">
                {selectedRecommendation?.reasoning_summary ??
                  "Select a recommendation to inspect the proposed treatment, evidence posture, and control rationale."}
              </p>
            </article>

            <article className="quartz-card">
              <p className="quartz-card-eyebrow">Impact if approved</p>
              <div className="quartz-reasoning-list">
                <div className="quartz-reasoning-item">
                  <strong>
                    {selectedJournal
                      ? `Journal ${selectedJournal.journal_number}`
                      : "Draft pending"}
                  </strong>
                  <span className="quartz-mini-meta">
                    {selectedJournal
                      ? `Debits ${selectedJournal.total_debits} • Credits ${selectedJournal.total_credits}`
                      : "Approving the recommendation will produce or refresh the linked draft journal."}
                  </span>
                </div>
                <div className="quartz-reasoning-item">
                  <strong>Posting posture</strong>
                  <span className="quartz-mini-meta">
                    {closeRun.operatingMode.journalPostingAvailable
                      ? "This close run can post internally or generate an ERP package after approval."
                      : "Journal posting is restricted for this operating mode until ledger baselines are available."}
                  </span>
                </div>
              </div>
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}

function buildQueuedRecommendationMessage(
  result: {
    queued_count: number;
    skipped_document_ids: readonly string[];
    skipped_documents: readonly {
      document_id: string;
      reason: string;
      status: string;
    }[];
  },
  force: boolean,
): string {
  const importedGlSkipCount = result.skipped_documents.filter(
    (document) => document.status === "represented_in_imported_gl",
  ).length;
  const existingRecommendationSkipCount = result.skipped_documents.filter(
    (document) => document.status === "existing_recommendation",
  ).length;

  if (force) {
    return result.queued_count > 0
      ? `${result.queued_count} recommendation regeneration job(s) queued.`
      : importedGlSkipCount > 0
        ? "No regeneration jobs were queued because the imported GL already represents the eligible documents."
        : "No eligible documents were found for recommendation regeneration.";
  }

  if (result.queued_count > 0) {
    return `${result.queued_count} recommendation job(s) queued.`;
  }
  if (importedGlSkipCount > 0 && existingRecommendationSkipCount === 0) {
    return "No new recommendations were queued because the imported GL already represents the eligible documents.";
  }
  if (existingRecommendationSkipCount > 0 && importedGlSkipCount === 0) {
    return "No new recommendations were queued because the document already has an active recommendation. Use Regenerate recommendations to replace it.";
  }
  if (result.skipped_document_ids.length > 0) {
    return "No new recommendations were queued because the eligible documents were already represented in the current close-run state.";
  }
  return "No eligible documents were found for recommendation generation.";
}

function selectNextRecommendationId(
  recommendations: readonly RecommendationSummary[],
): string | null {
  return (
    recommendations.find((recommendation) => isPendingRecommendationStatus(recommendation.status))
      ?.id ??
    recommendations[0]?.id ??
    null
  );
}

function isPendingRecommendationStatus(status: string): boolean {
  return status.includes("pending") || status.includes("queued") || status.includes("draft");
}

function buildRecommendationMetrics(recommendations: readonly RecommendationSummary[]): Readonly<{
  approvedCount: number;
  coverage: number;
  pendingCount: number;
}> {
  if (recommendations.length === 0) {
    return {
      approvedCount: 0,
      coverage: 0,
      pendingCount: 0,
    };
  }

  const approvedCount = recommendations.filter((recommendation) =>
    recommendation.status.includes("approved"),
  ).length;
  const pendingCount = recommendations.filter((recommendation) =>
    isPendingRecommendationStatus(recommendation.status),
  ).length;
  const reviewedCount = recommendations.length - pendingCount;

  return {
    approvedCount,
    coverage: Math.round((reviewedCount / recommendations.length) * 100),
    pendingCount,
  };
}

function deriveRecommendationAmount(
  recommendation: RecommendationSummary | null,
  linkedJournal: JournalSummary | null,
): string {
  if (linkedJournal !== null) {
    return linkedJournal.total_debits;
  }
  if (recommendation === null) {
    return "—";
  }
  return "Pending";
}

function resolveConfidenceTone(score: number): "error" | "success" | "warning" {
  if (score >= 0.9) {
    return "success";
  }
  if (score >= 0.75) {
    return "warning";
  }
  return "error";
}

function formatConfidenceLabel(score: number): string {
  if (score >= 0.9) {
    return "Verified";
  }
  if (score >= 0.75) {
    return "High";
  }
  return "Review";
}

function resolveRecommendationStatusTone(
  status: string,
): "error" | "neutral" | "success" | "warning" {
  if (status.includes("reject") || status.includes("exception")) {
    return "error";
  }
  if (status.includes("approve") || status.includes("applied")) {
    return "success";
  }
  if (isPendingRecommendationStatus(status)) {
    return "warning";
  }
  return "neutral";
}

function formatPostingLabel(posting: JournalPostingSummary): string {
  const baseLabel = JOURNAL_POSTING_TARGET_LABELS[posting.posting_target];
  if (posting.provider) {
    return `${baseLabel} • ${posting.provider.replaceAll("_", " ")}`;
  }
  return baseLabel;
}

function formatLabel(value: string): string {
  return value
    .replaceAll("-", "_")
    .split("_")
    .filter((part) => part.length > 0)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function resolveRecommendationsErrorMessage(error: unknown): string {
  if (error instanceof RecommendationApiError || error instanceof CloseRunApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return "The recommendations workspace request failed. Reload and try again.";
}
