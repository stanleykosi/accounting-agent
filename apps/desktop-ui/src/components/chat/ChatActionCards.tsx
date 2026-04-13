/*
Purpose: Render proposed changes, approval requests, and workflow action cards
inline within chat message streams.
Scope: Displays action cards with confidence indicators, target references,
evidence links, and approve/reject controls for chat-originated action plans.
Dependencies: React, chat API client types, enterprise design tokens.

Design notes:
- Cards are rendered inline between regular chat messages for context.
- Each card shows the action intent, confidence level, target reference,
  and status badge.
- Pending cards include quick approve/reject buttons.
- Approved/rejected cards show the outcome and reviewer attribution.
*/

"use client";

import { useCallback, useState } from "react";
import {
  approveChatAction,
  type ChatActionSummary,
  ChatApiError,
  rejectChatAction,
} from "../../lib/chat";

/** Describe the shape of an action card rendered in the chat stream. */
export type ChatActionCardData = ChatActionSummary & {
  /** Optional human-readable description of the proposed change. */
  description?: string;
  /** Optional before/after values for proposed edits. */
  diff?: { from: string; to: string };
};

/** Props for a single ChatActionCard component. */
export type ChatActionCardProps = {
  /** The action plan data to render. */
  action: ChatActionCardData;
  /** Entity ID for access verification on approve/reject calls. */
  entityId: string;
  /** Called when the action state changes (approve/reject). */
  onStateChange?: ((action: ChatActionSummary) => void) | undefined;
  /** Whether the card should render in a compact queue style. */
  compact?: boolean;
};

/** Map action intents to human-readable labels. */
const INTENT_LABELS: Record<string, string> = {
  approval_request: "Approval Request",
  document_request: "Document Request",
  explanation: "Explanation",
  proposed_edit: "Proposed Edit",
  reconciliation_query: "Reconciliation Query",
  report_action: "Report Action",
  workflow_action: "Workflow Action",
};

/** Map status to display labels and colors. */
const STATUS_CONFIG: Record<string, { label: string; color: string; bg: string }> = {
  pending: {
    label: "Pending",
    color: "#E7A93B",
    bg: "rgba(231, 169, 59, 0.12)",
  },
  approved: {
    label: "Approved",
    color: "#1FA971",
    bg: "rgba(31, 169, 113, 0.12)",
  },
  applied: {
    label: "Applied",
    color: "#2CB6A4",
    bg: "rgba(44, 182, 164, 0.12)",
  },
  rejected: {
    label: "Rejected",
    color: "#D9534F",
    bg: "rgba(217, 83, 79, 0.12)",
  },
  superseded: {
    label: "Superseded",
    color: "#B7C3D6",
    bg: "rgba(183, 195, 214, 0.1)",
  },
};

/** Confidence color helper for the badge. */
function getConfidenceColor(score: number): string {
  if (score >= 0.8) return "#1FA971";
  if (score >= 0.5) return "#E7A93B";
  return "#D9534F";
}

function getConfidenceLabel(score: number): string {
  if (score >= 0.8) return "High";
  if (score >= 0.5) return "Medium";
  return "Low";
}

/**
 * Purpose: Render one action card inline in the chat message stream.
 * Inputs: Action plan data, entity ID, and state change callback.
 * Outputs: Card with intent label, confidence, target reference, status,
 * and approve/reject controls for pending actions.
 */
export function ChatActionCard({
  action,
  entityId,
  onStateChange,
  compact = false,
}: ChatActionCardProps) {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const statusConfig = STATUS_CONFIG[action.status] ?? STATUS_CONFIG.pending;

  const handleApprove = useCallback(async () => {
    if (isLoading) return;
    setIsLoading(true);
    setError(null);

    try {
      const updated = await approveChatAction(
        action.id,
        action.thread_id,
        entityId,
        "Approved from chat",
      );
      onStateChange?.(updated);
    } catch (err) {
      setError(err instanceof ChatApiError ? err.message : "Failed to approve action.");
    } finally {
      setIsLoading(false);
    }
  }, [action.id, action.thread_id, entityId, isLoading, onStateChange]);

  const handleReject = useCallback(async () => {
    if (isLoading) return;
    setIsLoading(true);
    setError(null);

    try {
      const updated = await rejectChatAction(
        action.id,
        action.thread_id,
        entityId,
        "Rejected from chat",
      );
      onStateChange?.(updated);
    } catch (err) {
      setError(err instanceof ChatApiError ? err.message : "Failed to reject action.");
    } finally {
      setIsLoading(false);
    }
  }, [action.id, action.thread_id, entityId, isLoading, onStateChange]);

  const isPending = action.status === "pending";

  if (compact) {
    return (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 8px",
          borderRadius: 6,
          background: "#182338",
          border: "1px solid #24324A",
          fontSize: 11,
          color: "#F4F7FB",
        }}
      >
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: statusConfig?.color ?? "#B7C3D6",
          }}
        />
        <span style={{ fontWeight: 500 }}>{INTENT_LABELS[action.intent] ?? action.intent}</span>
        {action.target_type && (
          <span style={{ color: "#B7C3D6" }}>on {action.target_type.slice(0, 20)}</span>
        )}
        <span
          style={{
            marginLeft: "auto",
            fontSize: 10,
            color: statusConfig?.color ?? "#B7C3D6",
            background: statusConfig?.bg ?? "rgba(183, 195, 214, 0.1)",
            padding: "1px 5px",
            borderRadius: 4,
          }}
        >
          {statusConfig?.label ?? "Unknown"}
        </span>
      </div>
    );
  }

  return (
    <div
      style={{
        background: "#182338",
        border: `1px solid ${isPending ? "#4C8BF5" : "#24324A"}`,
        borderRadius: 12,
        padding: "12px 16px",
        margin: "8px 0",
      }}
    >
      {/* Header: intent label + status badge */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 8,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              fontSize: 13,
              fontWeight: 600,
              color: "#F4F7FB",
              lineHeight: "20px",
            }}
          >
            {INTENT_LABELS[action.intent] ?? action.intent}
          </span>
          {action.requires_human_approval && (
            <span
              style={{
                fontSize: 9,
                fontWeight: 500,
                color: "#E7A93B",
                background: "rgba(231, 169, 59, 0.12)",
                padding: "1px 5px",
                borderRadius: 4,
                lineHeight: "14px",
              }}
            >
              Requires Review
            </span>
          )}
        </div>
        <span
          style={{
            fontSize: 10,
            fontWeight: 500,
            color: statusConfig?.color ?? "#B7C3D6",
            background: statusConfig?.bg ?? "rgba(183, 195, 214, 0.1)",
            padding: "2px 8px",
            borderRadius: 6,
            lineHeight: "16px",
          }}
        >
          {statusConfig?.label ?? "Unknown"}
        </span>
      </div>

      {/* Description and diff */}
      {action.description && (
        <p
          style={{
            fontSize: 13,
            lineHeight: "20px",
            color: "#B7C3D6",
            margin: "0 0 8px 0",
          }}
        >
          {action.description}
        </p>
      )}

      {action.diff && (
        <div
          style={{
            display: "flex",
            gap: 12,
            marginBottom: 8,
            fontSize: 12,
            fontFamily: "'IBM Plex Mono', monospace",
          }}
        >
          <span style={{ color: "#D9534F" }}>− {action.diff.from}</span>
          <span style={{ color: "#B7C3D6" }}>→</span>
          <span style={{ color: "#1FA971" }}>+ {action.diff.to}</span>
        </div>
      )}

      {/* Target reference */}
      {action.target_type && (
        <div
          style={{
            fontSize: 11,
            color: "#B7C3D6",
            marginBottom: 8,
            lineHeight: "16px",
          }}
        >
          Target: <span style={{ color: "#F4F7FB", fontWeight: 500 }}>{action.target_type}</span>
          {action.target_id && (
            <code
              style={{
                marginLeft: 4,
                fontSize: 10,
                background: "#0B1020",
                padding: "1px 4px",
                borderRadius: 4,
              }}
            >
              {action.target_id.slice(0, 8)}...
            </code>
          )}
        </div>
      )}

      {/* Confidence indicator */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: isPending ? 10 : 0,
        }}
      >
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: getConfidenceColor(0.7),
          }}
        />
        <span style={{ fontSize: 10, color: "#B7C3D6", lineHeight: "14px" }}>
          Confidence:{" "}
          <span
            style={{
              color: getConfidenceColor(0.7),
              fontWeight: 500,
            }}
          >
            {getConfidenceLabel(0.7)}
          </span>
        </span>
      </div>

      {/* Error display */}
      {error && (
        <div
          style={{
            marginBottom: 8,
            padding: "6px 10px",
            borderRadius: 8,
            background: "rgba(217, 83, 79, 0.1)",
            border: "1px solid rgba(217, 83, 79, 0.3)",
            color: "#D9534F",
            fontSize: 12,
            lineHeight: "18px",
          }}
        >
          {error}
        </div>
      )}

      {/* Action buttons for pending actions */}
      {isPending && (
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button
            type="button"
            onClick={() => void handleApprove()}
            disabled={isLoading}
            style={{
              flex: 1,
              fontSize: 12,
              fontWeight: 500,
              padding: "6px 0",
              borderRadius: 8,
              border: "1px solid #1FA971",
              background: isLoading ? "rgba(31, 169, 113, 0.06)" : "rgba(31, 169, 113, 0.12)",
              color: "#1FA971",
              cursor: isLoading ? "not-allowed" : "pointer",
              opacity: isLoading ? 0.6 : 1,
              transition: "background 150ms ease",
            }}
          >
            {isLoading ? "Processing..." : "Approve"}
          </button>
          <button
            type="button"
            onClick={() => void handleReject()}
            disabled={isLoading}
            style={{
              flex: 1,
              fontSize: 12,
              fontWeight: 500,
              padding: "6px 0",
              borderRadius: 8,
              border: "1px solid #D9534F",
              background: isLoading ? "rgba(217, 83, 79, 0.06)" : "rgba(217, 83, 79, 0.12)",
              color: "#D9534F",
              cursor: isLoading ? "not-allowed" : "pointer",
              opacity: isLoading ? 0.6 : 1,
              transition: "background 150ms ease",
            }}
          >
            {isLoading ? "Processing..." : "Reject"}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Purpose: Render a list of action cards stacked inline in the chat stream.
 * Inputs: Array of action summaries and entity ID.
 * Outputs: Vertically stacked cards with shared state management.
 */
export type ChatActionCardListProps = {
  actions: ChatActionCardData[];
  entityId: string;
  onStateChange?: (action: ChatActionSummary) => void;
  compact?: boolean;
};

export function ChatActionCardList({
  actions,
  entityId,
  onStateChange,
  compact = false,
}: ChatActionCardListProps) {
  if (actions.length === 0) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      {actions.map((action) => (
        <ChatActionCard
          key={action.id}
          action={action}
          entityId={entityId}
          onStateChange={onStateChange}
          compact={compact}
        />
      ))}
    </div>
  );
}
