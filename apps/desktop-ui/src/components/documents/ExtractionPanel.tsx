/*
Purpose: Render extracted-field context for the selected document review queue item.
Scope: Field summaries, confidence cues, issue context, and reviewer action controls beside the queue table.
Dependencies: Shared review UI primitives plus document review types and page-provided callbacks.
*/

"use client";

import { ConfidenceBadge, DiffViewer } from "@accounting-ai-agent/ui";
import { type ReactElement } from "react";
import {
  type DocumentReviewQueueItem,
  type EvidenceReference,
  type ReviewDraftDecision,
} from "../../lib/documents";

export type ExtractionPanelProps = {
  draftDecision: ReviewDraftDecision | null;
  onOpenEvidence: (input: {
    references: readonly EvidenceReference[];
    sourceLabel: string;
    title: string;
  }) => void;
  onReviewAction: (documentId: string, decision: ReviewDraftDecision) => void;
  selectedDocument: DocumentReviewQueueItem | null;
};

/**
 * Purpose: Display extraction context and reviewer controls for one selected queue document.
 * Inputs: Selected document summary, optional local draft decision, and action/evidence callbacks.
 * Outputs: A detail panel aligned with the evidence-first review workflow.
 * Behavior: Presents field evidence links before actions so reviewers can validate context first.
 */
export function ExtractionPanel({
  draftDecision,
  onOpenEvidence,
  onReviewAction,
  selectedDocument,
}: Readonly<ExtractionPanelProps>): ReactElement {
  if (selectedDocument === null) {
    return (
      <section className="extraction-panel-empty" aria-live="polite">
        <h3>Select a Document</h3>
        <p>Choose a row from the queue to inspect extracted fields and supporting evidence.</p>
      </section>
    );
  }

  return (
    <section className="extraction-panel-shell" aria-label="Extraction panel">
      <header className="extraction-panel-header">
        <div>
          <p className="eyebrow">Selected Document</p>
          <h3>{selectedDocument.originalFilename}</h3>
          <p>
            {formatLabel(selectedDocument.documentType)} • {formatLabel(selectedDocument.status)} •{" "}
            {selectedDocument.mimeType}
          </p>
        </div>
        <div className="review-pane-badge-row">
          <ConfidenceBadge
            score={selectedDocument.classificationConfidence}
            tone={selectedDocument.confidenceBand}
          />
          {draftDecision ? (
            <span className="review-draft-chip">Draft decision: {formatLabel(draftDecision)}</span>
          ) : null}
        </div>
      </header>

      <div className="panel-action-row review-linked-actions">
        <button
          className="secondary-button compact-action"
          onClick={() =>
            onOpenEvidence({
              references: selectedDocument.evidenceRefs,
              sourceLabel: selectedDocument.originalFilename,
              title: "Document evidence",
            })
          }
          type="button"
        >
          Open all evidence
        </button>
        <button
          className="secondary-button compact-action"
          disabled={!selectedDocument.hasException}
          onClick={() => onReviewAction(selectedDocument.id, "approved")}
          type="button"
        >
          Approve
        </button>
        <button
          className="secondary-button compact-action"
          disabled={!selectedDocument.hasException}
          onClick={() => onReviewAction(selectedDocument.id, "rejected")}
          type="button"
        >
          Reject
        </button>
        <button
          className="secondary-button compact-action"
          onClick={() => onReviewAction(selectedDocument.id, "needs_info")}
          type="button"
        >
          Request info
        </button>
      </div>

      {draftDecision !== null ? (
        <DiffViewer
          afterLabel="Draft routing"
          afterValue={buildDraftDecisionSummary(draftDecision)}
          beforeLabel="Current review state"
          beforeValue={buildCurrentReviewSummary(selectedDocument)}
          title="Decision impact"
        />
      ) : null}

      <div className="exception-summary-row">
        {selectedDocument.issueTypes.length === 0 ? (
          <span className="queue-chip positive">No active exception</span>
        ) : (
          selectedDocument.issueTypes.map((issueType) => (
            <span
              className={`queue-chip ${selectedDocument.issueSeverity === "blocking" ? "blocking" : "warning"}`}
              key={`${selectedDocument.id}:panel:${issueType}`}
            >
              {formatLabel(issueType)}
            </span>
          ))
        )}
      </div>

      {selectedDocument.extractedFields.length === 0 ? (
        <div className="status-banner warning" role="status">
          No structured fields were extracted for this document yet. Review source evidence before
          approving the queue item.
        </div>
      ) : (
        <ul className="extraction-field-list">
          {selectedDocument.extractedFields.map((field) => (
            <li
              className={`extraction-field-row ${field.confidence !== null && field.confidence < 0.75 ? "is-low-confidence" : ""}`}
              key={field.id}
            >
              <div>
                <h4>{field.label}</h4>
                <p>{field.value}</p>
              </div>
              <div className="field-row-actions">
                <ConfidenceBadge score={field.confidence} size="compact" />
                <button
                  className="secondary-button compact-action"
                  onClick={() =>
                    onOpenEvidence({
                      references: field.evidenceRefs,
                      sourceLabel: selectedDocument.originalFilename,
                      title: `${field.label} evidence`,
                    })
                  }
                  type="button"
                >
                  View evidence
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Purpose: Convert canonical snake_case values to short title-case labels for UI chips.
 * Inputs: Canonical workflow or issue label value.
 * Outputs: Human-readable display text.
 * Behavior: Applies deterministic string transformation without maintaining a second label map.
 */
function formatLabel(value: string): string {
  return value
    .split("_")
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function buildCurrentReviewSummary(selectedDocument: DocumentReviewQueueItem): string {
  const issues =
    selectedDocument.issueTypes.length === 0
      ? "No active exceptions"
      : selectedDocument.issueTypes.map(formatLabel).join(", ");

  return [
    `Status: ${formatLabel(selectedDocument.status)}`,
    `Period state: ${
      selectedDocument.periodState === "in_period"
        ? "In period"
        : selectedDocument.periodState === "out_of_period"
          ? "Outside period"
          : "Not detected"
    }`,
    `Exceptions: ${issues}`,
    `OCR required: ${selectedDocument.ocrRequired ? "Yes" : "No"}`,
  ].join("\n");
}

function buildDraftDecisionSummary(draftDecision: ReviewDraftDecision): string {
  const outcomeMap: Readonly<Record<ReviewDraftDecision, string>> = {
    approved: "Move the document toward phase-gate clearance after review logging.",
    needs_info: "Keep the document in review and request more supporting evidence.",
    rejected: "Preserve the block and record explicit reviewer rejection context.",
  };

  return [
    `Draft action: ${formatLabel(draftDecision)}`,
    `Routing: ${outcomeMap[draftDecision]}`,
    "Audit trail: Evidence links and source metadata remain attached.",
  ].join("\n");
}
