/*
Purpose: Render the reconciliation match-review queue table.
Scope: Filter tabs, item rows with match status, amounts, confidence, exceptions, and
       reviewer action buttons. Mirrors the document review table pattern adapted for
       reconciliation items.
Dependencies: Shared UI primitives (SurfaceCard, EvidenceDrawer), reconciliation domain types.
*/

"use client";

import { useCallback, type ReactElement } from "react";
import {
  type ReconciliationItemSummary,
  type ReconciliationReviewFilter,
  formatMatchStatusLabel,
} from "../../lib/reconciliation";

type MatchReviewTableProps = {
  activeFilter: ReconciliationReviewFilter;
  items: ReadonlyArray<ReconciliationItemSummary>;
  queueCounts: {
    unresolved: number;
    matched: number;
    exception: number;
    unmatched: number;
  };
  onFilterChange: (filter: ReconciliationReviewFilter) => void;
  onSelectItem: (itemId: string) => void;
  onOpenEvidence: (itemId: string) => void;
  onReviewAction: (itemId: string, action: string) => void;
  selectedItemId: string | null;
};

const FILTER_TABS: ReadonlyArray<{ key: ReconciliationReviewFilter; label: string }> = [
  { key: "all", label: "All" },
  { key: "unresolved", label: "Unresolved" },
  { key: "matched", label: "Matched" },
  { key: "exception", label: "Exceptions" },
  { key: "unmatched", label: "Unmatched" },
];

/**
 * Purpose: Render a filterable table of reconciliation match results for reviewer disposition.
 * Inputs: Active filter, item array, queue counts, and callback handlers.
 * Outputs: A table with one row per reconciliation item showing source, match status, amounts,
 *          and action buttons.
 * Behavior: Rows are highlighted when selected. Filter tabs update the visible item set.
 */
export function MatchReviewTable({
  activeFilter,
  items,
  queueCounts,
  onFilterChange,
  onSelectItem,
  onOpenEvidence,
  onReviewAction,
  selectedItemId,
}: Readonly<MatchReviewTableProps>): ReactElement {
  const handleRowClick = useCallback(
    (itemId: string) => {
      onSelectItem(itemId);
    },
    [onSelectItem],
  );

  return (
    <div className="reconciliation-review-table">
      {/* Filter tabs */}
      <div className="review-filter-tabs" role="tablist" aria-label="Reconciliation item filters">
        {FILTER_TABS.map((tab) => {
          const count = getTabCount(tab.key, queueCounts);
          const isActive = activeFilter === tab.key;
          return (
            <button
              key={tab.key}
              role="tab"
              aria-selected={isActive}
              className={`review-filter-tab ${isActive ? "active" : ""}`}
              onClick={() => onFilterChange(tab.key)}
            >
              {tab.label}
              {count > 0 && <span className="review-filter-tab-count">{count}</span>}
            </button>
          );
        })}
      </div>

      {/* Table */}
      {items.length === 0 ? (
        <div className="review-empty-state" role="status">
          <p className="form-helper">
            No items match the selected filter.{" "}
            {activeFilter !== "all" && (
              <button
                className="link-button"
                onClick={() => onFilterChange("all")}
              >
                Show all items
              </button>
            )}
          </p>
        </div>
      ) : (
        <div className="review-table-container">
          <table className="review-data-table">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Reference</th>
                <th scope="col">Amount</th>
                <th scope="col">Match Status</th>
                <th scope="col">Difference</th>
                <th scope="col">Disposition</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr
                  key={item.id}
                  className={`review-data-row ${selectedItemId === item.id ? "selected" : ""} ${
                    item.matchStatus === "exception" ? "row-exception" : ""
                  } ${item.matchStatus === "unmatched" ? "row-unmatched" : ""}`}
                  onClick={() => handleRowClick(item.id)}
                  tabIndex={0}
                  role="row"
                  aria-selected={selectedItemId === item.id}
                >
                  <td>
                    <span className="source-type-label">{formatSourceType(item.sourceType)}</span>
                  </td>
                  <td>
                    <span className="source-ref-text" title={item.sourceRef}>
                      {truncate(item.sourceRef, 32)}
                    </span>
                  </td>
                  <td className="amount-cell">{formatAmount(item.amount)}</td>
                  <td>
                    <MatchStatusBadge status={item.matchStatus} />
                  </td>
                  <td className={`amount-cell ${isNonZeroDifference(item.differenceAmount) ? "diff-warning" : ""}`}>
                    {formatAmount(item.differenceAmount)}
                  </td>
                  <td>
                    {item.disposition ? (
                      <DispositionBadge disposition={item.disposition} />
                    ) : item.requiresDisposition ? (
                      <span className="disposition-pending-badge">Pending</span>
                    ) : (
                      <span className="disposition-not-required-badge">N/A</span>
                    )}
                  </td>
                  <td className="action-cell">
                    <button
                      className="action-btn action-btn-evidence"
                      onClick={(e) => {
                        e.stopPropagation();
                        onOpenEvidence(item.id);
                      }}
                      title="View evidence"
                    >
                      Evidence
                    </button>
                    {item.requiresDisposition && item.disposition === null && (
                      <button
                        className="action-btn action-btn-resolve"
                        onClick={(e) => {
                          e.stopPropagation();
                          onReviewAction(item.id, "resolved");
                        }}
                        title="Mark as resolved"
                      >
                        Resolve
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/**
 * Purpose: Render a color-coded badge for the match status of one reconciliation item.
 * Inputs: Match status string.
 * Outputs: A span element with status-appropriate styling.
 */
function MatchStatusBadge({ status }: Readonly<{ status: string }>): ReactElement {
  const colorMap: Readonly<Record<string, { bg: string; fg: string }>> = {
    matched: { bg: "#dcfce7", fg: "#166534" },
    partially_matched: { bg: "#fef9c3", fg: "#854d0e" },
    exception: { bg: "#fee2e2", fg: "#991b1b" },
    unmatched: { bg: "#f3f4f6", fg: "#374151" },
  };
  const colors = colorMap[status] ?? { bg: "#f3f4f6", fg: "#374151" };

  return (
    <span
      className="match-status-badge"
      style={{ backgroundColor: colors.bg, color: colors.fg }}
    >
      {formatMatchStatusLabel(status)}
    </span>
  );
}

/**
 * Purpose: Render a badge for the reviewer disposition of one reconciliation item.
 * Inputs: Disposition action string.
 * Outputs: A span element with disposition-appropriate styling.
 */
function DispositionBadge({ disposition }: Readonly<{ disposition: string }>): ReactElement {
  const colorMap: Readonly<Record<string, { bg: string; fg: string }>> = {
    resolved: { bg: "#dcfce7", fg: "#166534" },
    adjusted: { bg: "#dbeafe", fg: "#1e40af" },
    accepted_as_is: { bg: "#f3f4f6", fg: "#374151" },
    escalated: { bg: "#fef3c7", fg: "#92400e" },
    pending_info: { bg: "#ede9fe", fg: "#5b21b6" },
  };
  const colors = colorMap[disposition] ?? { bg: "#f3f4f6", fg: "#374151" };

  return (
    <span
      className="disposition-badge"
      style={{ backgroundColor: colors.bg, color: colors.fg }}
    >
      {disposition.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase())}
    </span>
  );
}

/**
 * Purpose: Return the count value for a filter tab.
 * Inputs: Filter key and queue counts object.
 * Outputs: The numeric count for that tab.
 */
function getTabCount(
  key: ReconciliationReviewFilter,
  counts: { unresolved: number; matched: number; exception: number; unmatched: number },
): number {
  switch (key) {
    case "unresolved":
      return counts.unresolved;
    case "matched":
      return counts.matched;
    case "exception":
      return counts.exception;
    case "unmatched":
      return counts.unmatched;
    default:
      return 0;
  }
}

/**
 * Purpose: Format a source type enum into a human-readable label.
 * Inputs: Source type string.
 * Outputs: A display label.
 */
function formatSourceType(sourceType: string): string {
  const labels: Readonly<Record<string, string>> = {
    bank_statement_line: "Bank Line",
    ledger_transaction: "Ledger Tx",
    recommendation: "Recommendation",
    external_balance: "Ext Balance",
    manual_adjustment: "Manual Adj",
  };
  return labels[sourceType] ?? sourceType;
}

/**
 * Purpose: Format a decimal string as a currency amount.
 * Inputs: Amount string.
 * Outputs: Formatted currency string with 2 decimal places.
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
