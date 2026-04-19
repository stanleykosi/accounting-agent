/*
Purpose: Render extracted-field context for the selected document review queue item.
Scope: Field summaries, evidence inspection, reviewer notes, persisted review actions,
and extracted-field correction controls beside the queue table.
Dependencies: Shared review UI primitives plus document review types and page-provided callbacks.
*/

"use client";

import { ConfidenceBadge } from "@accounting-ai-agent/ui";
import { useState, type ChangeEvent, type ReactElement } from "react";
import {
  type AutoTransactionMatchSummary,
  type DocumentVerificationChecklist,
  type DocumentReviewQueueItem,
  type EvidenceReference,
} from "../../lib/documents";

export type ExtractionPanelProps = {
  actionNote: string;
  deleteMutationDocumentId: string | null;
  fieldMutationId: string | null;
  checklist: DocumentVerificationChecklist | null;
  onDeleteDocument: (documentId: string) => Promise<void> | void;
  onReparseDocument: (documentId: string) => Promise<void> | void;
  onFieldCorrection: (input: {
    correctedType: string;
    correctedValue: string;
    fieldId: string;
  }) => Promise<void> | void;
  onChecklistChange: (
    field: keyof DocumentVerificationChecklist,
    nextValue: boolean,
  ) => void;
  onNoteChange: (value: string) => void;
  onOpenEvidence: (input: {
    references: readonly EvidenceReference[];
    sourceLabel: string;
    title: string;
  }) => void;
  onReviewAction: (
    documentId: string,
    decision: "approved" | "rejected" | "needs_info",
  ) => Promise<void> | void;
  reparseMutationDocumentId: string | null;
  reviewMutationDocumentId: string | null;
  selectedDocument: DocumentReviewQueueItem | null;
};

export function ExtractionPanel({
  actionNote,
  deleteMutationDocumentId,
  fieldMutationId,
  checklist,
  onDeleteDocument,
  onReparseDocument,
  onFieldCorrection,
  onChecklistChange,
  onNoteChange,
  onOpenEvidence,
  onReviewAction,
  reparseMutationDocumentId,
  reviewMutationDocumentId,
  selectedDocument,
}: Readonly<ExtractionPanelProps>): ReactElement {
  const [editingFieldId, setEditingFieldId] = useState<string | null>(null);
  const [fieldDraftValue, setFieldDraftValue] = useState("");

  if (selectedDocument === null) {
    return (
      <section className="extraction-panel-empty" aria-live="polite">
        <h3>Select a Document</h3>
        <p>Choose a row from the queue to inspect extracted fields and supporting evidence.</p>
      </section>
    );
  }

  const isReviewMutating = reviewMutationDocumentId === selectedDocument.id;
  const isDeleteMutating = deleteMutationDocumentId === selectedDocument.id;
  const isReparseMutating = reparseMutationDocumentId === selectedDocument.id;
  const approvalReady =
    checklist !== null && checklist.complete && checklist.authorized && checklist.period;

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
          {selectedDocument.latestExtraction ? (
            <span className="review-draft-chip">
              Extraction v{selectedDocument.latestExtraction.versionNo}
              {selectedDocument.latestExtraction.approvedVersion ? " approved" : " pending"}
            </span>
          ) : null}
        </div>
      </header>

      {selectedDocument.latestExtraction?.autoApproved ? (
        <div className="status-banner success" role="status">
          System-approved in reduced interruption mode because extraction and transaction-linking
          checks passed without blockers.
        </div>
      ) : null}

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
          disabled={isReviewMutating || isDeleteMutating || isReparseMutating || !approvalReady}
          onClick={() => {
            void onReviewAction(selectedDocument.id, "approved");
          }}
          type="button"
        >
          {isReviewMutating ? "Saving..." : "Approve"}
        </button>
        <button
          className="secondary-button compact-action"
          disabled={isReviewMutating || isDeleteMutating || isReparseMutating}
          onClick={() => {
            void onReviewAction(selectedDocument.id, "rejected");
          }}
          type="button"
        >
          Reject
        </button>
        <button
          className="secondary-button compact-action"
          disabled={isReviewMutating || isDeleteMutating || isReparseMutating}
          onClick={() => {
            void onReviewAction(selectedDocument.id, "needs_info");
          }}
          type="button"
        >
          Request info
        </button>
        <button
          className="secondary-button compact-action"
          disabled={isReviewMutating || isDeleteMutating || isReparseMutating}
          onClick={() => {
            void onReparseDocument(selectedDocument.id);
          }}
          type="button"
        >
          {isReparseMutating ? "Reparsing..." : "Reparse document"}
        </button>
        <button
          className="secondary-button compact-action"
          disabled={isReviewMutating || isDeleteMutating || isReparseMutating}
          onClick={() => {
            void onDeleteDocument(selectedDocument.id);
          }}
          type="button"
        >
          {isDeleteMutating ? "Deleting..." : "Delete document"}
        </button>
      </div>

      <label className="form-label" htmlFor="document-review-note">
        Reviewer note
      </label>
      <textarea
        className="native-textarea"
        id="document-review-note"
        onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onNoteChange(event.target.value)}
        placeholder="Add approval context, rejection rationale, or correction notes."
        rows={3}
        value={actionNote}
      />

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

      <section className="dashboard-row">
        <strong className="close-run-row-title">Phase 1 review checklist</strong>
        <p className="form-helper">
          The accountant workflow requires completeness, authorization, and period alignment
          before Collection can move into Processing. Transaction linking below is supporting
          evidence when it is available.
        </p>
        <div className="entity-card-list">
          <label className="entity-card">
            <input
              checked={checklist?.complete ?? false}
              onChange={(event) => {
                onChecklistChange("complete", event.target.checked);
              }}
              type="checkbox"
            />
            <strong>Complete</strong>
            <p className="form-helper">All required source support is present for this document.</p>
          </label>
          <label className="entity-card">
            <input
              checked={checklist?.authorized ?? false}
              onChange={(event) => {
                onChecklistChange("authorized", event.target.checked);
              }}
              type="checkbox"
            />
            <strong>Authorized</strong>
            <p className="form-helper">The document is approved and valid for posting.</p>
          </label>
          <label className="entity-card">
            <input
              checked={checklist?.period ?? false}
              onChange={(event) => {
                onChecklistChange("period", event.target.checked);
              }}
              type="checkbox"
            />
            <strong>Correct period</strong>
            <p className="form-helper">The source belongs to this close-run reporting window.</p>
          </label>
        </div>
        {!approvalReady ? (
          <p className="form-helper">
            Approval stays locked until all three verification controls are confirmed.
          </p>
        ) : null}
      </section>

      {selectedDocument.latestExtraction?.autoTransactionMatch ? (
        <section className="dashboard-row">
          <strong className="close-run-row-title">Auto transaction link</strong>
          <div className="dashboard-row-list">
            <article className="dashboard-row">
              <div className="close-run-row-header">
                <div>
                  <strong className="close-run-row-title">
                    {formatAutoTransactionMatchStatus(
                      selectedDocument.latestExtraction.autoTransactionMatch.status,
                    )}
                  </strong>
                  <p className="close-run-row-meta">
                    {formatAutoTransactionMatchMeta(
                      selectedDocument.latestExtraction.autoTransactionMatch,
                    )}
                  </p>
                </div>
              </div>
              {selectedDocument.latestExtraction.autoTransactionMatch.reasons.map((reason) => (
                <p className="form-helper" key={`${selectedDocument.id}:${reason}`}>
                  {reason}
                </p>
              ))}
            </article>
          </div>
        </section>
      ) : null}

      {selectedDocument.openIssues.length > 0 ? (
        <section className="dashboard-row">
          <strong className="close-run-row-title">Open verification findings</strong>
          <div className="dashboard-row-list">
            {selectedDocument.openIssues.map((issue) => (
              <article className="dashboard-row" key={issue.id}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">{formatLabel(issue.issueType)}</strong>
                    <p className="close-run-row-meta">
                      {formatLabel(issue.severity)} • {formatLabel(issue.status)}
                    </p>
                  </div>
                </div>
                <p className="form-helper">
                  {typeof issue.details.reason === "string"
                    ? issue.details.reason
                    : "This finding must be resolved or explicitly dispositioned before approval."}
                </p>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      <dl className="entity-meta-grid document-review-summary-grid">
        <div>
          <dt>Period state</dt>
          <dd>{formatPeriodState(selectedDocument.periodState)}</dd>
        </div>
        <div>
          <dt>OCR required</dt>
          <dd>{selectedDocument.ocrRequired ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Extraction schema</dt>
          <dd>
            {selectedDocument.latestExtraction
              ? `${selectedDocument.latestExtraction.schemaName} ${selectedDocument.latestExtraction.schemaVersion}`
              : "Not available"}
          </dd>
        </div>
        <div>
          <dt>Extraction review</dt>
          <dd>
            {selectedDocument.latestExtraction?.approvedVersion
              ? "Approved"
              : selectedDocument.latestExtraction
                ? "Pending review"
                : "Not extracted"}
          </dd>
        </div>
      </dl>

      {selectedDocument.extractedFields.length === 0 ? (
        <div className="status-banner warning" role="status">
          No structured fields were extracted for this document yet. Review source evidence before
          approving the queue item.
        </div>
      ) : (
        <ul className="extraction-field-list">
          {selectedDocument.extractedFields.map((field) => {
            const isEditing = editingFieldId === field.id;
            const isSaving = fieldMutationId === field.id;

            return (
              <li
                className={`extraction-field-row ${field.confidence !== null && field.confidence < 0.75 ? "is-low-confidence" : ""}`}
                key={field.id}
              >
                <div>
                  <h4>
                    {field.label}
                    {field.isHumanCorrected ? (
                      <span className="review-draft-chip">Human corrected</span>
                    ) : null}
                  </h4>
                  {isEditing ? (
                    <textarea
                      className="native-textarea"
                      onChange={(event: ChangeEvent<HTMLTextAreaElement>) =>
                        setFieldDraftValue(event.target.value)
                      }
                      rows={2}
                      value={fieldDraftValue}
                    />
                  ) : (
                    <p>{field.value}</p>
                  )}
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
                  {isEditing ? (
                    <>
                      <button
                        className="secondary-button compact-action"
                        disabled={isSaving || fieldDraftValue.trim().length === 0}
                        onClick={() => {
                          void onFieldCorrection({
                            correctedType: field.fieldType,
                            correctedValue: fieldDraftValue,
                            fieldId: field.id,
                          });
                          setEditingFieldId(null);
                        }}
                        type="button"
                      >
                        {isSaving ? "Saving..." : "Save"}
                      </button>
                      <button
                        className="secondary-button compact-action"
                        disabled={isSaving}
                        onClick={() => {
                          setEditingFieldId(null);
                          setFieldDraftValue("");
                        }}
                        type="button"
                      >
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button
                      className="secondary-button compact-action"
                      onClick={() => {
                        setEditingFieldId(field.id);
                        setFieldDraftValue(field.value === "Not detected" ? "" : field.value);
                      }}
                      type="button"
                    >
                      Edit field
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function formatLabel(value: string): string {
  return value
    .split("_")
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatPeriodState(value: DocumentReviewQueueItem["periodState"]): string {
  if (value === "in_period") {
    return "Within close-run period";
  }
  if (value === "out_of_period") {
    return "Outside close-run period";
  }
  return "Period not detected";
}

function formatAutoTransactionMatchStatus(
  value: AutoTransactionMatchSummary["status"],
): string {
  if (value === "not_applicable") {
    return "Not applicable";
  }
  if (value === "pending_evidence") {
    return "Waiting for bank evidence";
  }
  if (value === "matched") {
    return "Matched automatically";
  }
  return "Match not found";
}

function formatAutoTransactionMatchMeta(
  summary: AutoTransactionMatchSummary,
): string {
  if (summary.status === "not_applicable") {
    return "No separate transaction link is required for this document type.";
  }
  if (summary.status === "pending_evidence") {
    return "Upload and parse a bank statement later if you want deterministic transaction linking.";
  }

  const fragments: string[] = [];
  if (summary.matchedDocumentFilename) {
    fragments.push(summary.matchedDocumentFilename);
  }
  if (summary.matchedLineNo !== null) {
    fragments.push(`line ${summary.matchedLineNo}`);
  }
  if (summary.matchedAmount) {
    fragments.push(summary.matchedAmount);
  }
  if (summary.matchedDate) {
    fragments.push(summary.matchedDate);
  }
  if (summary.score !== null) {
    fragments.push(`score ${Math.round(summary.score * 100)}%`);
  }
  if (fragments.length === 0) {
    return "No deterministic counterpart details were captured.";
  }
  return fragments.join(" • ");
}
