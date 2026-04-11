/*
Purpose: Render the document exception queue table with filters and reviewer decisions.
Scope: Exception-focused filtering, row selection, confidence indicators, and inline review actions.
Dependencies: Document review domain helpers and React event handlers provided by the close-run documents page.
*/

"use client";

import { type ReactElement } from "react";
import {
  formatConfidenceLabel,
  type DocumentReviewFilter,
  type DocumentReviewQueueCounts,
  type DocumentReviewQueueItem,
  type ReviewDraftDecision,
} from "../../lib/documents";

export type DocumentReviewTableProps = {
  activeFilter: DocumentReviewFilter;
  items: readonly DocumentReviewQueueItem[];
  onFilterChange: (filter: DocumentReviewFilter) => void;
  onOpenEvidence: (documentId: string) => void;
  onReviewAction: (documentId: string, decision: ReviewDraftDecision) => void;
  onSelectDocument: (documentId: string) => void;
  queueCounts: DocumentReviewQueueCounts;
  reviewDecisions: Readonly<Record<string, ReviewDraftDecision | undefined>>;
  selectedDocumentId: string | null;
};

type QueueFilterDefinition = {
  filter: DocumentReviewFilter;
  label: string;
};

const filterDefinitions: readonly QueueFilterDefinition[] = [
  { filter: "all", label: "All documents" },
  { filter: "low_confidence", label: "Low confidence" },
  { filter: "blocked", label: "Blocked" },
  { filter: "duplicate", label: "Duplicates" },
  { filter: "wrong_period", label: "Wrong period" },
];

/**
 * Purpose: Render the collection-phase document review queue and in-row reviewer controls.
 * Inputs: Queue rows, filter state, current selection, and review action callbacks.
 * Outputs: A dense table optimized for side-by-side queue and evidence workflows.
 * Behavior: Keeps filters explicit and surfaces exception chips before reviewer action buttons.
 */
export function DocumentReviewTable({
  activeFilter,
  items,
  onFilterChange,
  onOpenEvidence,
  onReviewAction,
  onSelectDocument,
  queueCounts,
  reviewDecisions,
  selectedDocumentId,
}: Readonly<DocumentReviewTableProps>): ReactElement {
  return (
    <section className="document-review-table-shell" aria-label="Document review queue">
      <div className="document-review-filter-row" role="tablist" aria-label="Queue filters">
        {filterDefinitions.map((definition) => {
          const isActive = activeFilter === definition.filter;
          return (
            <button
              aria-selected={isActive}
              className={`queue-filter-chip ${isActive ? "active" : ""}`}
              key={definition.filter}
              onClick={() => onFilterChange(definition.filter)}
              role="tab"
              type="button"
            >
              <span>{definition.label}</span>
              <strong>{queueCounts[definition.filter]}</strong>
            </button>
          );
        })}
      </div>

      {items.length === 0 ? (
        <div className="status-banner warning" role="status">
          No documents match this queue filter. Switch filters or upload additional source files.
        </div>
      ) : null}

      {items.length > 0 ? (
        <div className="document-review-table-container">
          <table className="document-review-table">
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col">Confidence</th>
                <th scope="col">Exceptions</th>
                <th scope="col">Period</th>
                <th scope="col">Reviewer actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const selected = selectedDocumentId === item.id;
                const draftDecision = reviewDecisions[item.id];
                return (
                  <tr
                    aria-selected={selected}
                    className={selected ? "selected" : ""}
                    key={item.id}
                    onClick={() => onSelectDocument(item.id)}
                  >
                    <td>
                      <div className="document-primary-cell">
                        <button
                          className="document-link-button"
                          onClick={(event) => {
                            event.stopPropagation();
                            onSelectDocument(item.id);
                          }}
                          type="button"
                        >
                          {item.originalFilename}
                        </button>
                        <p>
                          {formatLabel(item.documentType)} • {formatLabel(item.status)}
                        </p>
                      </div>
                    </td>
                    <td>
                      <span className={`confidence-pill ${item.confidenceBand}`}>
                        {formatConfidenceLabel(item.classificationConfidence)}
                      </span>
                    </td>
                    <td>
                      <div className="queue-chip-list">
                        {item.issueTypes.length === 0 ? (
                          <span className="queue-chip positive">No exception</span>
                        ) : (
                          item.issueTypes.map((issueType) => (
                            <span
                              className={`queue-chip ${item.issueSeverity === "blocking" ? "blocking" : "warning"}`}
                              key={`${item.id}:${issueType}`}
                            >
                              {formatLabel(issueType)}
                            </span>
                          ))
                        )}
                      </div>
                    </td>
                    <td>
                      <span
                        className={`period-state-pill ${
                          item.periodState === "out_of_period"
                            ? "out"
                            : item.periodState === "in_period"
                              ? "in"
                              : "unknown"
                        }`}
                      >
                        {item.periodState === "out_of_period"
                          ? "Outside period"
                          : item.periodState === "in_period"
                            ? "In period"
                            : "Not detected"}
                      </span>
                    </td>
                    <td>
                      <div className="review-action-group">
                        <button
                          className="secondary-button compact-action"
                          onClick={(event) => {
                            event.stopPropagation();
                            onOpenEvidence(item.id);
                          }}
                          type="button"
                        >
                          Evidence
                        </button>
                        <button
                          className="secondary-button compact-action"
                          disabled={!item.hasException}
                          onClick={(event) => {
                            event.stopPropagation();
                            onReviewAction(item.id, "approved");
                          }}
                          type="button"
                        >
                          Approve
                        </button>
                        <button
                          className="secondary-button compact-action"
                          disabled={!item.hasException}
                          onClick={(event) => {
                            event.stopPropagation();
                            onReviewAction(item.id, "rejected");
                          }}
                          type="button"
                        >
                          Reject
                        </button>
                        <button
                          className="secondary-button compact-action"
                          onClick={(event) => {
                            event.stopPropagation();
                            onReviewAction(item.id, "needs_info");
                          }}
                          type="button"
                        >
                          Request info
                        </button>
                      </div>
                      {draftDecision ? (
                        <p className="review-decision-label">Draft: {formatLabel(draftDecision)}</p>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

/**
 * Purpose: Render queue chip labels from canonical snake_case workflow values.
 * Inputs: Canonical enum-like string values from the document queue model.
 * Outputs: Title-cased text suitable for chips and compact labels.
 * Behavior: Keeps labeling deterministic without introducing duplicated lookup maps.
 */
function formatLabel(value: string): string {
  return value
    .split("_")
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}
