/*
Purpose: Provide an action-capable chat composer that extends the basic message
input with action intent detection, approval buttons, and proposed-edit controls.
Scope: Used within the ChatRail component to replace the basic MessageComposer
when the chat thread supports action routing (Step 35).
Dependencies: React, existing chat API client, enterprise design tokens.

Behavior:
- Renders the standard message input with an enhanced action mode toggle
- When action mode is enabled, messages route through the action endpoint
- Displays pending action badges and quick-approve/reject controls
- Surfaces confidence indicators for detected action intents
*/

"use client";

import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  approveChatAction,
  type ChatActionResponse,
  type ChatActionSummary,
  ChatApiError,
  listThreadActions,
  rejectChatAction,
  sendChatAction,
  sendChatMessage,
} from "../../lib/chat";

/** Describe the props for the ActionComposer component. */
export type ActionComposerProps = {
  /** Current chat thread ID used for action routing. */
  threadId: string;
  /** Entity workspace ID for access verification. */
  entityId: string;
  /** Called when a message is successfully sent. */
  onMessageSent: (response: ChatActionResponse) => void;
  /** Called when an action is approved, rejected, or errored. */
  onActionStateChange?: (action: ChatActionSummary) => void;
  /** Whether the composer should render in a disabled/loading state. */
  disabled?: boolean;
};

/** Map action intents to human-readable labels. */
const ACTION_INTENT_LABELS: Record<string, string> = {
  approval_request: "Approve/Reject",
  document_request: "Request Document",
  explanation: "Explain",
  proposed_edit: "Proposed Edit",
  reconciliation_query: "Reconciliation Query",
  report_action: "Report Action",
  workflow_action: "Workflow Action",
};

/** Map action intents to confidence color indicators. */
const ACTION_INTENT_COLORS: Record<string, string> = {
  approval_request: "#4C8BF5",
  document_request: "#E7A93B",
  explanation: "#B7C3D6",
  proposed_edit: "#D9534F",
  reconciliation_query: "#5AA4FF",
  report_action: "#2CB6A4",
  workflow_action: "#4C8BF5",
};

/**
 * Purpose: Render a chat composer with action mode, pending action badges,
 * and inline approve/reject controls.
 * Inputs: Thread ID, entity ID, and callbacks for message/action events.
 * Outputs: Interactive composer with input, action toggle, and pending actions.
 */
export function ActionComposer({
  threadId,
  entityId,
  onMessageSent,
  onActionStateChange,
  disabled = false,
}: ActionComposerProps) {
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [actionMode, setActionMode] = useState(false);
  const [pendingActions, setPendingActions] = useState<ChatActionSummary[]>([]);
  const [loadingActions, setLoadingActions] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const loadPendingActions = useCallback(async () => {
    try {
      const actions = await listThreadActions(threadId, entityId);
      setPendingActions(actions);
    } catch (err) {
      // Silently fail -- pending actions are a convenience, not required
      if (err instanceof ChatApiError && err.status !== 404) {
        console.warn("Failed to load pending chat actions:", err);
      }
    }
  }, [threadId, entityId]);

  useEffect(() => {
    void loadPendingActions();
  }, [loadPendingActions]);

  const handleSubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      const trimmed = inputValue.trim();
      if (!trimmed || isLoading || disabled) return;

      setIsLoading(true);
      setError(null);

      try {
        // Route through action endpoint when action mode is enabled,
        // otherwise fall back to standard read-only message endpoint
        let actionResponse: ChatActionResponse;
        if (actionMode) {
          actionResponse = await sendChatAction(threadId, entityId, trimmed);
        } else {
          const standardResponse = await sendChatMessage(threadId, entityId, trimmed);
          actionResponse = {
            message_id: standardResponse.message.id,
            content: standardResponse.message.content,
            action_plan: null,
            is_read_only: true,
          };
        }

        setInputValue("");
        onMessageSent(actionResponse);
      } catch (err) {
        const message =
          err instanceof ChatApiError ? err.message : "Failed to send message. Please try again.";
        setError(message);
      } finally {
        setIsLoading(false);
      }
    },
    [inputValue, isLoading, disabled, actionMode, threadId, entityId, onMessageSent],
  );

  const handleActionApproval = useCallback(
    async (actionId: string) => {
      if (loadingActions.has(actionId)) return;

      setLoadingActions((prev) => new Set(prev).add(actionId));
      try {
        const updated = await approveChatAction(actionId, threadId, entityId);
        setPendingActions((prev) => prev.filter((a) => a.id !== actionId));
        onActionStateChange?.(updated);
      } catch (err) {
        const message =
          err instanceof ChatApiError ? err.message : "Failed to approve action. Please try again.";
        setError(message);
      } finally {
        setLoadingActions((prev) => {
          const next = new Set(prev);
          next.delete(actionId);
          return next;
        });
      }
    },
    [threadId, entityId, loadingActions, onActionStateChange],
  );

  const handleActionRejection = useCallback(
    async (actionId: string) => {
      if (loadingActions.has(actionId)) return;

      setLoadingActions((prev) => new Set(prev).add(actionId));
      try {
        const updated = await rejectChatAction(
          actionId,
          threadId,
          entityId,
          "Rejected via quick action",
        );
        setPendingActions((prev) => prev.filter((a) => a.id !== actionId));
        onActionStateChange?.(updated);
      } catch (err) {
        const message =
          err instanceof ChatApiError ? err.message : "Failed to reject action. Please try again.";
        setError(message);
      } finally {
        setLoadingActions((prev) => {
          const next = new Set(prev);
          next.delete(actionId);
          return next;
        });
      }
    },
    [threadId, entityId, loadingActions, onActionStateChange],
  );

  const isSubmitting = isLoading || disabled;
  const hasInput = inputValue.trim().length > 0;

  return (
    <div
      style={{
        borderTop: "1px solid #24324A",
        background: "#121A2B",
        padding: "12px 16px",
      }}
    >
      {/* Pending actions strip */}
      {pendingActions.length > 0 && (
        <div
          style={{
            marginBottom: 8,
            display: "flex",
            gap: 6,
            overflowX: "auto",
            paddingBottom: 4,
          }}
        >
          {pendingActions.map((action) => (
            <div
              key={action.id}
              style={{
                flexShrink: 0,
                background: "#182338",
                border: "1px solid #24324A",
                borderRadius: 8,
                padding: "6px 10px",
                minWidth: 180,
                maxWidth: 240,
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginBottom: 4,
                }}
              >
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: ACTION_INTENT_COLORS[action.intent] ?? "#B7C3D6",
                    display: "inline-block",
                  }}
                />
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 500,
                    color: "#F4F7FB",
                    lineHeight: "16px",
                  }}
                >
                  {ACTION_INTENT_LABELS[action.intent] ?? action.intent}
                </span>
                {action.requires_human_approval && (
                  <span
                    style={{
                      fontSize: 9,
                      color: "#E7A93B",
                      background: "rgba(231, 169, 59, 0.12)",
                      padding: "1px 4px",
                      borderRadius: 4,
                      lineHeight: "14px",
                    }}
                  >
                    Review
                  </span>
                )}
              </div>
              <div
                style={{
                  display: "flex",
                  gap: 4,
                  marginTop: 4,
                }}
              >
                <button
                  type="button"
                  onClick={() => void handleActionApproval(action.id)}
                  disabled={loadingActions.has(action.id)}
                  style={{
                    flex: 1,
                    fontSize: 11,
                    fontWeight: 500,
                    padding: "3px 0",
                    borderRadius: 6,
                    border: "1px solid #1FA971",
                    background: "rgba(31, 169, 113, 0.12)",
                    color: "#1FA971",
                    cursor: loadingActions.has(action.id) ? "not-allowed" : "pointer",
                    opacity: loadingActions.has(action.id) ? 0.6 : 1,
                  }}
                >
                  {loadingActions.has(action.id) ? "..." : "Approve"}
                </button>
                <button
                  type="button"
                  onClick={() => void handleActionRejection(action.id)}
                  disabled={loadingActions.has(action.id)}
                  style={{
                    flex: 1,
                    fontSize: 11,
                    fontWeight: 500,
                    padding: "3px 0",
                    borderRadius: 6,
                    border: "1px solid #D9534F",
                    background: "rgba(217, 83, 79, 0.12)",
                    color: "#D9534F",
                    cursor: loadingActions.has(action.id) ? "not-allowed" : "pointer",
                    opacity: loadingActions.has(action.id) ? 0.6 : 1,
                  }}
                >
                  {loadingActions.has(action.id) ? "..." : "Reject"}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Error banner */}
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

      {/* Action mode toggle */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 8,
        }}
      >
        <button
          type="button"
          onClick={() => setActionMode((prev) => !prev)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11,
            fontWeight: 500,
            color: actionMode ? "#4C8BF5" : "#B7C3D6",
            background: actionMode ? "rgba(76, 139, 245, 0.1)" : "transparent",
            border: "none",
            borderRadius: 6,
            padding: "3px 8px",
            cursor: "pointer",
          }}
        >
          <span
            style={{
              width: 14,
              height: 14,
              borderRadius: 4,
              border: `1.5px solid ${actionMode ? "#4C8BF5" : "#B7C3D6"}`,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              background: actionMode ? "#4C8BF5" : "transparent",
            }}
          >
            {actionMode && (
              <svg width="8" height="8" viewBox="0 0 8 8" fill="none">
                <path
                  d="M1 4L3 6L7 2"
                  stroke="white"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </span>
          Action mode
        </button>
        {actionMode && (
          <span
            style={{
              fontSize: 10,
              color: "#E7A93B",
              background: "rgba(231, 169, 59, 0.1)",
              padding: "2px 6px",
              borderRadius: 4,
            }}
          >
            Actions route through review
          </span>
        )}
      </div>

      {/* Input form */}
      <form
        onSubmit={(event) => {
          void handleSubmit(event);
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-end",
          }}
        >
          <textarea
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
              if (event.key === "Enter" && !event.shiftKey && !event.metaKey) {
                event.preventDefault();
                if (hasInput && !isSubmitting) {
                  void handleSubmit(event);
                }
              }
            }}
            placeholder={
              actionMode
                ? "Type a message with action intent (e.g. approve, change, post...)"
                : "Ask about this close run..."
            }
            disabled={isSubmitting}
            rows={1}
            style={{
              flex: 1,
              fontSize: 13,
              lineHeight: "20px",
              color: "#F4F7FB",
              background: "#0B1020",
              border: "1px solid #24324A",
              borderRadius: 10,
              padding: "8px 12px",
              resize: "none",
              outline: "none",
              fontFamily: "inherit",
              maxHeight: 120,
            }}
          />
          <button
            type="submit"
            disabled={!hasInput || isSubmitting}
            style={{
              width: 32,
              height: 32,
              borderRadius: 8,
              border: "none",
              background: hasInput && !isSubmitting ? "#4C8BF5" : "#24324A",
              color: hasInput && !isSubmitting ? "#fff" : "#B7C3D6",
              cursor: hasInput && !isSubmitting ? "pointer" : "not-allowed",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              transition: "background 150ms ease",
            }}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M2 8L14 2L10 8L14 14L2 8Z" fill="currentColor" />
            </svg>
          </button>
        </div>
      </form>
    </div>
  );
}
