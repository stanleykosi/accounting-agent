/*
Purpose: Render the production recommendation and journal review workspace for one close run.
Scope: Recommendation generation, reviewer disposition, journal approval/apply, and selected journal detail.
Dependencies: Hosted same-origin recommendation APIs and shared desktop surface cards.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import { use, useCallback, useEffect, useState, type ReactElement } from "react";
import {
  approveJournal,
  approveRecommendation,
  applyJournal,
  buildJournalPostingDownloadPath,
  JOURNAL_POSTING_TARGET_LABELS,
  generateRecommendations,
  listJournals,
  listRecommendations,
  readJournal,
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

export default function RecommendationsPage({
  params,
}: Readonly<RecommendationsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isGenerating, setIsGenerating] = useState(false);
  const [journals, setJournals] = useState<readonly JournalSummary[]>([]);
  const [queuedMessage, setQueuedMessage] = useState<string | null>(null);
  const [recommendations, setRecommendations] = useState<readonly RecommendationSummary[]>([]);
  const [selectedJournal, setSelectedJournal] = useState<JournalSummary | null>(null);

  const refreshWorkspace = useCallback(async (): Promise<void> => {
    setIsLoading(true);
    try {
      const [nextRecommendations, nextJournals] = await Promise.all([
        listRecommendations(entityId, closeRunId),
        listJournals(entityId, closeRunId),
      ]);
      setRecommendations(nextRecommendations);
      setJournals(nextJournals);
      setSelectedJournal((currentSelectedJournal) => {
        if (currentSelectedJournal === null) {
          return nextJournals[0] ?? null;
        }
        return nextJournals.find((journal) => journal.id === currentSelectedJournal.id) ?? null;
      });
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId]);

  useEffect(() => {
    void refreshWorkspace();
  }, [refreshWorkspace]);

  async function handleGenerateRecommendations(): Promise<void> {
    setIsGenerating(true);
    try {
      const result = await generateRecommendations(entityId, closeRunId);
      setQueuedMessage(
        result.queued_count > 0
          ? `${result.queued_count} recommendation job(s) queued.`
          : "No eligible documents were found for recommendation generation.",
      );
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationErrorMessage(error));
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleSelectJournal(journalId: string): Promise<void> {
    try {
      const journal = await readJournal(entityId, closeRunId, journalId);
      setSelectedJournal(journal);
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationErrorMessage(error));
    }
  }

  async function handleRecommendationAction(
    recommendationId: string,
    action: "approve" | "reject",
  ): Promise<void> {
    try {
      if (action === "approve") {
        await approveRecommendation(entityId, closeRunId, recommendationId, "Approved in review workspace");
      } else {
        await rejectRecommendation(entityId, closeRunId, recommendationId, "Rejected in review workspace");
      }
      await refreshWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationErrorMessage(error));
    }
  }

  async function handleJournalAction(
    journalId: string,
    action: "approve" | "apply_internal" | "apply_external" | "reject",
  ): Promise<void> {
    try {
      if (action === "approve") {
        await approveJournal(entityId, closeRunId, journalId, "Approved in journal workspace");
      } else if (action === "apply_internal") {
        await applyJournal(
          entityId,
          closeRunId,
          journalId,
          "internal_ledger",
          "Posted to internal ledger in journal workspace",
        );
      } else if (action === "apply_external") {
        await applyJournal(
          entityId,
          closeRunId,
          journalId,
          "external_erp_package",
          "Generated ERP import package in journal workspace",
        );
      } else {
        await rejectJournal(entityId, closeRunId, journalId, "Rejected in journal workspace");
      }
      await refreshWorkspace();
      await handleSelectJournal(journalId);
    } catch (error: unknown) {
      setErrorMessage(resolveRecommendationErrorMessage(error));
    }
  }

  return (
    <div className="app-shell recommendations-page">
      <section className="hero-grid close-run-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Accounting Review</p>
          <h1>Recommendations and journals</h1>
          <p className="lede">
            Execute the Processing phase: classify approved documents, verify GL coding, and review
            journals with cost centre, department, and project dimensions before posting.
          </p>
        </div>

        <SurfaceCard title="Workflow Actions" subtitle="Processing phase" tone="accent">
          <div className="integration-action-stack">
            <button
              className="primary-button"
              disabled={isGenerating}
              onClick={() => {
                void handleGenerateRecommendations();
              }}
              type="button"
            >
              {isGenerating ? "Queueing..." : "Generate recommendations"}
            </button>
            <p className="form-helper">
              Recommendation generation runs asynchronously for eligible documents in this close
              run. Review and journal materialization happen below.
            </p>
          </div>
        </SurfaceCard>
      </section>

      {queuedMessage ? (
        <div className="status-banner success" role="status">
          {queuedMessage}
        </div>
      ) : null}

      {errorMessage ? (
        <div className="status-banner danger" role="alert">
          {errorMessage}
        </div>
      ) : null}

      <section className="content-grid">
        <SurfaceCard title="Recommendations" subtitle={`${recommendations.length} items`}>
          {isLoading ? <p className="form-helper">Loading recommendations...</p> : null}
          {!isLoading && recommendations.length === 0 ? (
            <p className="form-helper">
              No recommendations exist yet. Upload and process documents, then queue generation.
            </p>
          ) : null}
          <div className="dashboard-row-list">
            {recommendations.map((recommendation) => (
              <article className="dashboard-row" key={recommendation.id}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">
                      {recommendation.recommendation_type.replaceAll("_", " ")}
                    </strong>
                    <p className="close-run-row-meta">
                      {recommendation.status.replaceAll("_", " ")} • Confidence{" "}
                      {Math.round(recommendation.confidence * 100)}%
                    </p>
                  </div>
                </div>
                <p className="form-helper">{recommendation.reasoning_summary}</p>
                <div className="close-run-link-row">
                  <button
                    className="secondary-button"
                    onClick={() => {
                      void handleRecommendationAction(recommendation.id, "approve");
                    }}
                    type="button"
                  >
                    Approve
                  </button>
                  <button
                    className="secondary-button"
                    onClick={() => {
                      void handleRecommendationAction(recommendation.id, "reject");
                    }}
                    type="button"
                  >
                    Reject
                  </button>
                </div>
              </article>
            ))}
          </div>
        </SurfaceCard>

        <SurfaceCard title="Journals" subtitle={`${journals.length} generated`}>
          {isLoading ? <p className="form-helper">Loading journals...</p> : null}
          {!isLoading && journals.length === 0 ? (
            <p className="form-helper">
              Journal drafts appear here after recommendations are approved.
            </p>
          ) : null}
          <div className="dashboard-row-list">
            {journals.map((journal) => (
              <article className="dashboard-row" key={journal.id}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">{journal.journal_number}</strong>
                    <p className="close-run-row-meta">
                      {journal.status.replaceAll("_", " ")} • Debits {journal.total_debits} • Credits{" "}
                      {journal.total_credits}
                    </p>
                    {journal.postings[0] ? (
                      <p className="form-helper">
                        Posted via {formatPostingLabel(journal.postings[0])} on{" "}
                        {new Date(journal.postings[0].posted_at).toLocaleString()}
                      </p>
                    ) : null}
                  </div>
                </div>
                <p className="form-helper">{journal.description}</p>
                <div className="close-run-link-row">
                  <button
                    className="secondary-button"
                    onClick={() => {
                      void handleSelectJournal(journal.id);
                    }}
                    type="button"
                  >
                    Inspect
                  </button>
                  <button
                    className="secondary-button"
                    disabled={journal.status !== "draft" && journal.status !== "pending_review"}
                    onClick={() => {
                      void handleJournalAction(journal.id, "approve");
                    }}
                    type="button"
                  >
                    Approve
                  </button>
                  <button
                    className="secondary-button"
                    disabled={journal.status !== "approved" || journal.postings.length > 0}
                    onClick={() => {
                      void handleJournalAction(journal.id, "apply_internal");
                    }}
                    type="button"
                  >
                    Post internally
                  </button>
                  <button
                    className="secondary-button"
                    disabled={journal.status !== "approved" || journal.postings.length > 0}
                    onClick={() => {
                      void handleJournalAction(journal.id, "apply_external");
                    }}
                    type="button"
                  >
                    Create ERP CSV
                  </button>
                  <button
                    className="secondary-button"
                    disabled={journal.status === "applied" || journal.postings.length > 0}
                    onClick={() => {
                      void handleJournalAction(journal.id, "reject");
                    }}
                    type="button"
                  >
                    Reject
                  </button>
                </div>
              </article>
            ))}
          </div>
        </SurfaceCard>
      </section>

      <SurfaceCard title="Selected Journal Detail" subtitle={selectedJournal?.journal_number ?? "No journal selected"}>
        {selectedJournal === null ? (
          <p className="form-helper">Select a journal to inspect its lines and status.</p>
        ) : (
          <div className="dashboard-row-list">
            <article className="dashboard-row">
              <div className="close-run-row-header">
                <div>
                  <strong className="close-run-row-title">{selectedJournal.description}</strong>
                  <p className="close-run-row-meta">
                    {selectedJournal.status.replaceAll("_", " ")} • {selectedJournal.posting_date}
                  </p>
                </div>
              </div>
              <div className="entity-card-list">
                {selectedJournal.lines.map((line) => (
                  <div className="entity-card" key={line.id}>
                    <strong>
                      {line.line_no}. {line.account_code}
                    </strong>
                    <p className="form-helper">
                      {line.line_type} • {line.amount}
                      {line.description ? ` • ${line.description}` : ""}
                    </p>
                    {Object.keys(line.dimensions).length > 0 ? (
                      <p className="form-helper">
                        {Object.entries(line.dimensions)
                          .map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`)
                          .join(" • ")}
                      </p>
                    ) : null}
                    {line.reference ? (
                      <p className="form-helper">Reference: {line.reference}</p>
                    ) : null}
                  </div>
                ))}
              </div>
              <div className="entity-card-list" style={{ marginTop: "20px" }}>
                {selectedJournal.postings.length === 0 ? (
                  <div className="entity-card">
                    <strong>No posting recorded yet</strong>
                    <p className="form-helper">
                      Choose whether this approved journal should land in the internal ledger or
                      produce an ERP import package.
                    </p>
                  </div>
                ) : (
                  selectedJournal.postings.map((posting) => (
                    <div className="entity-card" key={posting.id}>
                      <strong>{formatPostingLabel(posting)}</strong>
                      <p className="form-helper">
                        {posting.status.replaceAll("_", " ")} •{" "}
                        {new Date(posting.posted_at).toLocaleString()}
                      </p>
                      {posting.provider ? (
                        <p className="form-helper">
                          Provider/package format: {posting.provider.replaceAll("_", " ")}
                        </p>
                      ) : null}
                      {posting.note ? <p className="form-helper">{posting.note}</p> : null}
                      {posting.artifact_filename ? (
                        <a
                          className="secondary-button"
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
                  ))
                )}
              </div>
            </article>
          </div>
        )}
      </SurfaceCard>
    </div>
  );
}

function resolveRecommendationErrorMessage(error: unknown): string {
  if (error instanceof RecommendationApiError) {
    return error.message;
  }
  return "The accounting review request failed. Reload and try again.";
}

function formatPostingLabel(posting: JournalPostingSummary): string {
  const baseLabel = JOURNAL_POSTING_TARGET_LABELS[posting.posting_target];
  if (posting.provider) {
    return `${baseLabel} • ${posting.provider.replaceAll("_", " ")}`;
  }
  return baseLabel;
}
