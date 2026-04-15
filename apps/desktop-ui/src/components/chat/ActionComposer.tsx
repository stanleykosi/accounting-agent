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

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  approveChatAction,
  type ChatAttachmentIntent,
  type ChatActionResponse,
  type ChatActionSummary,
  ChatApiError,
  listThreadActions,
  rejectChatAction,
  sendChatAction,
  sendChatActionWithAttachments,
  sendChatMessage,
} from "../../lib/chat";

/** Describe the props for the ActionComposer component. */
export type ActionComposerProps = {
  /** Current chat thread ID used for action routing. */
  threadId: string;
  /** Entity workspace ID for access verification. */
  entityId: string;
  /** Optional close run scope; attachments use this to default routing intent. */
  closeRunId?: string | undefined;
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
  closeRunId,
  onMessageSent,
  onActionStateChange,
  disabled = false,
}: ActionComposerProps) {
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [actionMode, setActionMode] = useState(true);
  const [attachmentIntent, setAttachmentIntent] = useState<ChatAttachmentIntent>(
    closeRunId ? "source_documents" : "chart_of_accounts",
  );
  const [attachments, setAttachments] = useState<readonly File[]>([]);
  const [pendingActions, setPendingActions] = useState<ChatActionSummary[]>([]);
  const [loadingActions, setLoadingActions] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const availableAttachmentIntents = useMemo(
    () =>
      closeRunId
        ? ([
            { label: "Source documents", value: "source_documents" },
            { label: "Chart of accounts", value: "chart_of_accounts" },
          ] satisfies ReadonlyArray<{ label: string; value: ChatAttachmentIntent }>)
        : ([{ label: "Chart of accounts", value: "chart_of_accounts" }] satisfies ReadonlyArray<{
            label: string;
            value: ChatAttachmentIntent;
          }>),
    [closeRunId],
  );

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
      if ((trimmed.length === 0 && attachments.length === 0) || isLoading || disabled) return;

      setIsLoading(true);
      setError(null);

      try {
        // Route through action endpoint when action mode is enabled,
        // otherwise fall back to standard read-only message endpoint
        let actionResponse: ChatActionResponse;
        if (attachments.length > 0) {
          const attachmentRequest: {
            attachmentIntent: ChatAttachmentIntent;
            content?: string;
            files: readonly File[];
          } = {
            attachmentIntent,
            files: attachments,
          };
          if (trimmed.length > 0) {
            attachmentRequest.content = trimmed;
          }
          actionResponse = await sendChatActionWithAttachments(
            threadId,
            entityId,
            attachmentRequest,
          );
        } else if (actionMode) {
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
        setAttachments([]);
        if (fileInputRef.current !== null) {
          fileInputRef.current.value = "";
        }
        onMessageSent(actionResponse);
        await loadPendingActions();
      } catch (err) {
        const message =
          err instanceof ChatApiError ? err.message : "Failed to send message. Please try again.";
        setError(message);
      } finally {
        setIsLoading(false);
      }
    },
    [
      inputValue,
      attachments,
      isLoading,
      disabled,
      attachmentIntent,
      actionMode,
      threadId,
      entityId,
      loadPendingActions,
      onMessageSent,
    ],
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
  const hasInput = inputValue.trim().length > 0 || attachments.length > 0;

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
          Agent mode
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
            Tools, approvals, and audit aware
          </span>
        )}
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
          marginBottom: attachments.length > 0 ? 10 : 8,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <button
            onClick={() => fileInputRef.current?.click()}
            style={{
              background: "rgba(76, 139, 245, 0.12)",
              border: "1px solid rgba(76, 139, 245, 0.32)",
              borderRadius: 8,
              color: "#A8CBFF",
              cursor: "pointer",
              fontSize: 11,
              fontWeight: 600,
              padding: "5px 9px",
            }}
            type="button"
          >
            Attach files
          </button>
          <input
            accept=".pdf,.csv,.xlsx,.xls,.xlsm"
            multiple
            onChange={(event) => {
              const nextFiles = Array.from(event.target.files ?? []);
              setAttachments(nextFiles);
              setError(null);
              if (nextFiles.length > 0) {
                setActionMode(true);
              }
            }}
            ref={fileInputRef}
            style={{ display: "none" }}
            type="file"
          />
          {attachments.length > 0 ? (
            <select
              onChange={(event) =>
                setAttachmentIntent(event.target.value as ChatAttachmentIntent)
              }
              style={{
                background: "#0B1020",
                border: "1px solid #24324A",
                borderRadius: 8,
                color: "#D7E0ED",
                fontSize: 11,
                padding: "5px 9px",
              }}
              value={attachmentIntent}
            >
              {availableAttachmentIntents.map((option) => (
                <option key={option.value} value={option.value}>
                  Route as {option.label.toLowerCase()}
                </option>
              ))}
            </select>
          ) : null}
        </div>
        {attachments.length > 0 ? (
          <button
            onClick={() => {
              setAttachments([]);
              setError(null);
              if (fileInputRef.current !== null) {
                fileInputRef.current.value = "";
              }
            }}
            style={{
              background: "transparent",
              border: "none",
              color: "#8FA2BF",
              cursor: "pointer",
              fontSize: 11,
              padding: 0,
            }}
            type="button"
          >
            Clear
          </button>
        ) : null}
      </div>

      {attachments.length > 0 ? (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
            marginBottom: 10,
          }}
        >
          {attachments.map((file) => (
            <span
              key={`${file.name}:${file.size}`}
              style={{
                background: "#182338",
                border: "1px solid #24324A",
                borderRadius: 999,
                color: "#D7E0ED",
                display: "inline-flex",
                gap: 6,
                maxWidth: "100%",
                padding: "5px 9px",
              }}
            >
              <span
                style={{
                  fontSize: 11,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {file.name}
              </span>
              <span style={{ color: "#8FA2BF", fontSize: 10 }}>
                {formatByteSize(file.size)}
              </span>
            </span>
          ))}
        </div>
      ) : null}

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
                ? attachments.length > 0
                  ? "Tell the agent what to do with the attached files..."
                  : "Ask the agent to review, correct, approve, run workflow steps, or explain the current state..."
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

function formatByteSize(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}
