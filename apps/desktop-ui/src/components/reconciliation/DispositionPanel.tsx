/*
Purpose: Render the reviewer disposition panel for one selected reconciliation item.
Scope: Item detail header, matched counterpart list, explanation text, disposition action
       selector with reasoning input, and submit/reject buttons.
Dependencies: Shared review UI primitives, reconciliation domain types, and API helpers.
*/

"use client";

import { ConfidenceBadge } from "@accounting-ai-agent/ui";
import { useCallback, useState, type ReactElement } from "react";
import {
  type DispositionActionValue,
  type ReconciliationItemSummary,
  formatMatchStatusLabel,
} from "../../lib/reconciliation";

type DispositionPanelProps = {
  selectedItem: ReconciliationItemSummary | null;
  onDisposition: (
    itemId: string,
    disposition: DispositionActionValue,
    reason: string,
  ) => Promise<void>;
  onOpenEvidence: (itemId: string) => void;
};

const DISPOSITION_OPTIONS: ReadonlyArray<{ value: DispositionActionValue; label: string; description: string }> = [
  {
    value: "resolved",
    label: "Resolved",
    description: "Item has been investigated and resolved.",
  },
  {
    value: "adjusted",
    label: "Adjusted",
    description: "Item requires a journal or ledger adjustment.",
  },
  {
    value: "accepted_as_is",
    label: "Accepted as-is",
    description: "Item is acceptable without further action.",
  },
  {
    value: "escalated",
    label: "Escalated",
    description: "Item requires senior reviewer or controller input.",
  },
  {
    value: "pending_info",
    label: "Pending Info",
    description: "Additional documentation or context is needed.",
  },
];

/**
 * Purpose: Render a detail panel for one selected reconciliation item with disposition controls.
 * Inputs: Selected item, disposition handler, and evidence-opening handler.
 * Outputs: A card-style panel showing item detail, matched counterparts, explanation,
 *          and disposition form.
 * Behavior: The disposition form requires a reason before submission. Buttons are disabled
 *           when no item is selected or the form is submitting.
 */
export function DispositionPanel({
  selectedItem,
  onDisposition,
  onOpenEvidence,
}: Readonly<DispositionPanelProps>): ReactElement {
  const [selectedDisposition, setSelectedDisposition] = useState<DispositionActionValue>("resolved");
  const [reason, setReason] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmitDisposition = useCallback(async () => {
    if (selectedItem === null || reason.trim().length === 0) {
      return;
    }

    setIsSubmitting(true);
    setError(null);
    try {
      await onDisposition(selectedItem.id, selectedDisposition, reason.trim());
      setReason("");
    } catch (err: unknown) {
      setError(
        err instanceof Error
          ? err.message
          : "Failed to record disposition. Retry and try again.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }, [selectedItem, selectedDisposition, reason, onDisposition]);

  const handleOpenEvidence = useCallback(() => {
    if (selectedItem !== null) {
      onOpenEvidence(selectedItem.id);
    }
  }, [selectedItem, onOpenEvidence]);

  if (selectedItem === null) {
    return (
      <div className="disposition-panel">
        <p className="form-helper">
          Select a reconciliation item from the queue to view details and record a disposition.
        </p>
      </div>
    );
  }

  const canRecordDisposition = selectedItem.requiresDisposition && selectedItem.disposition === null;

  return (
    <div className="disposition-panel">
      {/* Header */}
      <div className="disposition-panel-header">
        <h3 className="disposition-panel-title">Item Detail</h3>
        <button className="secondary-button compact-action" onClick={handleOpenEvidence} type="button">
          View Evidence
        </button>
      </div>

      {/* Item summary */}
      <dl className="disposition-item-meta">
        <div>
          <dt>Source</dt>
          <dd>{formatSourceType(selectedItem.sourceType)}</dd>
        </div>
        <div>
          <dt>Reference</dt>
          <dd className="source-ref-full" title={selectedItem.sourceRef}>
            {selectedItem.sourceRef}
          </dd>
        </div>
        <div>
          <dt>Amount</dt>
          <dd>{formatAmount(selectedItem.amount)}</dd>
        </div>
        <div>
          <dt>Match Status</dt>
          <dd>{formatMatchStatusLabel(selectedItem.matchStatus)}</dd>
        </div>
        <div>
          <dt>Difference</dt>
          <dd className={isNonZeroDifference(selectedItem.differenceAmount) ? "diff-value-warning" : ""}>
            {formatAmount(selectedItem.differenceAmount)}
          </dd>
        </div>
        {selectedItem.explanation && (
          <div className="explanation-row">
            <dt>Explanation</dt>
            <dd className="explanation-text">{selectedItem.explanation}</dd>
          </div>
        )}
      </dl>

      {canRecordDisposition ? (
        <div className="status-banner info" role="status" style={{ marginBottom: "16px" }}>
          Choose what should happen next for this item. Use <strong>Resolved</strong> when
          the difference has been investigated and no further action is needed. Use{" "}
          <strong>Adjusted</strong> if a journal or ledger change is still required.
        </div>
      ) : null}

      {/* Matched counterparts */}
      {selectedItem.matchedTo.length > 0 && (
        <div className="matched-counterparts">
          <h4 className="counterparts-title">Matched Counterparts ({selectedItem.matchedTo.length})</h4>
          <ul className="counterparts-list">
            {selectedItem.matchedTo.map((cp, idx) => (
              <li key={idx} className="counterpart-item">
                <span className="counterpart-type">{formatSourceType(cp.sourceType)}</span>
                <span className="counterpart-ref">{truncate(cp.sourceRef, 28)}</span>
                {cp.amount !== null && (
                  <span className="counterpart-amount">{formatAmount(cp.amount)}</span>
                )}
                {cp.confidence !== null ? (
                  <ConfidenceBadge score={cp.confidence} size="compact" />
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Disposition form */}
      {canRecordDisposition ? (
        <div className="disposition-form">
          <h4 className="disposition-form-title">Record Disposition</h4>

          {/* Disposition options */}
          <div className="disposition-options" role="radiogroup" aria-label="Disposition action">
            {DISPOSITION_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={`disposition-option ${selectedDisposition === option.value ? "selected" : ""}`}
              >
                <input
                  type="radio"
                  name="disposition-action"
                  value={option.value}
                  checked={selectedDisposition === option.value}
                  onChange={() => setSelectedDisposition(option.value)}
                />
                <div className="disposition-option-content">
                  <span className="disposition-option-label">{option.label}</span>
                  <span className="disposition-option-desc">{option.description}</span>
                </div>
              </label>
            ))}
          </div>

          {/* Reason input */}
          <div className="form-field">
            <label htmlFor="disposition-reason" className="form-label">
              Reasoning <span className="required-star">*</span>
            </label>
            <textarea
              id="disposition-reason"
              className="text-input form-textarea"
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Explain what you found and why this action is correct..."
              maxLength={2000}
            />
            <p className="form-helper">{reason.length}/2000 characters</p>
          </div>

          {/* Error display */}
          {error && (
            <div className="status-banner danger" role="alert">
              {error}
            </div>
          )}

          {/* Submit button */}
          <button
            className="primary-button disposition-submit-btn"
            onClick={() => {
              void handleSubmitDisposition();
            }}
            disabled={isSubmitting || reason.trim().length === 0}
            type="button"
          >
            {isSubmitting ? "Submitting..." : `Submit: ${DISPOSITION_OPTIONS.find((o) => o.value === selectedDisposition)?.label}`}
          </button>
        </div>
      ) : null}

      {/* Already dispositioned */}
      {selectedItem.disposition !== null && (
        <div className="disposition-record">
          <p className="form-helper">
            This item was dispositioned as{" "}
            <strong>{formatDispositionLabel(selectedItem.disposition)}</strong>.
            {selectedItem.dispositionReason && (
              <>
                {" "}
                Reason: <em>{selectedItem.dispositionReason}</em>
              </>
            )}
          </p>
        </div>
      )}

      {/* Not requiring disposition */}
      {!selectedItem.requiresDisposition && (
        <div className="disposition-not-required">
          <p className="form-helper">This item does not require reviewer disposition.</p>
        </div>
      )}
    </div>
  );
}

/**
 * Purpose: Format a source type into a human-readable label.
 * Inputs: Source type string.
 * Outputs: Display label.
 */
function formatSourceType(sourceType: string): string {
  const labels: Readonly<Record<string, string>> = {
    bank_statement_line: "Bank Statement Line",
    ledger_transaction: "Ledger Transaction",
    recommendation: "Recommendation",
    external_balance: "External Balance",
    manual_adjustment: "Manual Adjustment",
  };
  return labels[sourceType] ?? sourceType;
}

/**
 * Purpose: Format a disposition action into a human-readable label.
 * Inputs: Disposition action string.
 * Outputs: Display label.
 */
function formatDispositionLabel(disposition: string): string {
  return disposition.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Purpose: Format a decimal string as a currency amount.
 * Inputs: Amount string.
 * Outputs: Formatted currency string.
 */
function formatAmount(value: string | null): string {
  if (value === null || value === undefined) return "—";
  const num = parseFloat(value);
  if (Number.isNaN(num)) return value;
  return num.toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/**
 * Purpose: Check if a difference amount is non-zero.
 * Inputs: Difference string.
 * Outputs: True if the absolute value exceeds 0.005.
 */
function isNonZeroDifference(value: string | null): boolean {
  if (value === null || value === undefined) return false;
  const num = parseFloat(value);
  return !Number.isNaN(num) && Math.abs(num) > 0.005;
}

/**
 * Purpose: Truncate a string to a maximum length with an ellipsis.
 * Inputs: String and maximum length.
 * Outputs: Truncated string.
 */
function truncate(value: string, maxLength: number): string {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, maxLength - 3)}...`;
}
