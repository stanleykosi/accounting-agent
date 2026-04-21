/*
Purpose: Provide an action-capable chat composer that extends the basic message
input with automatic agent routing, approval buttons, and proposed-edit controls.
Scope: Used within the ChatRail component as the single operator entrypoint for
grounded chat, workflow actions, and inline attachments.
Dependencies: React, existing chat API client, enterprise design tokens.

Behavior:
- Renders one unified message input for questions, explanations, and actions
- Messages route through the shared action endpoint so the backend chooses
  between read-only responses and deterministic tools
- Displays pending action badges and quick-approve/reject controls
*/

"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import {
  approveChatAction,
  type ChatAttachmentIntent,
  type ChatActionResponse,
  type ChatActionSummary,
  type ChatThreadWorkspace,
  ChatApiError,
  listThreadActions,
  rejectChatAction,
  sendChatAction,
  sendChatActionWithAttachments,
} from "../../lib/chat";

/** Describe the props for the ActionComposer component. */
export type ActionComposerProps = {
  /** Current chat thread ID used for action routing. */
  threadId: string;
  /** Entity workspace ID for access verification. */
  entityId: string;
  /** Optional close run scope; attachments use this to default routing intent. */
  closeRunId?: string | undefined;
  /** Live workspace state used to suggest natural next prompts. */
  workspace?: ChatThreadWorkspace | null;
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
  approval_request: "var(--quartz-secondary)",
  document_request: "var(--quartz-gold)",
  explanation: "var(--quartz-neutral)",
  proposed_edit: "var(--quartz-error)",
  reconciliation_query: "var(--quartz-secondary)",
  report_action: "var(--quartz-success)",
  workflow_action: "var(--quartz-secondary)",
};

/**
 * Purpose: Render a chat composer with action mode, pending action badges,
 * and inline approve/reject controls.
 * Inputs: Thread ID, entity ID, and callbacks for message/action events.
 * Outputs: Interactive composer with one input lane and pending action controls.
 */
export function ActionComposer({
  threadId,
  entityId,
  closeRunId,
  workspace = null,
  onMessageSent,
  onActionStateChange,
  disabled = false,
}: ActionComposerProps) {
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
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
  const starterPrompts = useMemo(
    () => buildStarterPrompts({ closeRunId, workspace }),
    [closeRunId, workspace],
  );

  const loadPendingActions = useCallback(async () => {
    if (threadId.trim().length === 0) {
      setPendingActions([]);
      return;
    }
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
      if (threadId.trim().length === 0) {
        setError("Create or select a chat thread before sending a message.");
        return;
      }

      setIsLoading(true);
      setError(null);

      try {
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
        } else {
          actionResponse = await sendChatAction(threadId, entityId, trimmed);
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
      threadId,
      entityId,
      loadPendingActions,
      onMessageSent,
    ],
  );

  const handleActionApproval = useCallback(
    async (actionId: string) => {
      if (loadingActions.has(actionId)) return;
      if (threadId.trim().length === 0) {
        setError("Select a chat thread before approving an action.");
        return;
      }

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
      if (threadId.trim().length === 0) {
        setError("Select a chat thread before rejecting an action.");
        return;
      }

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
        borderTop: "1px solid var(--quartz-border)",
        background: "var(--quartz-surface-low)",
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
                background: "var(--quartz-surface)",
                border: "1px solid var(--quartz-border)",
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
                    background: ACTION_INTENT_COLORS[action.intent] ?? "var(--quartz-neutral)",
                    display: "inline-block",
                  }}
                />
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 500,
                    color: "var(--quartz-ink)",
                    lineHeight: "16px",
                  }}
                >
                  {ACTION_INTENT_LABELS[action.intent] ?? action.intent}
                </span>
                {action.requires_human_approval && (
                  <span
                    style={{
                      fontSize: 9,
                      color: "var(--quartz-gold)",
                      background: "rgba(255, 251, 235, 0.92)",
                      border: "1px solid rgba(142, 115, 75, 0.22)",
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
                    border: "1px solid rgba(27, 67, 50, 0.18)",
                    background: "rgba(27, 67, 50, 0.08)",
                    color: "var(--quartz-success)",
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
                    border: "1px solid rgba(123, 45, 38, 0.22)",
                    background: "rgba(255, 218, 214, 0.72)",
                    color: "var(--quartz-error)",
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
            background: "var(--quartz-error-soft)",
            border: "1px solid rgba(123, 45, 38, 0.22)",
            color: "var(--quartz-error)",
            fontSize: 12,
            lineHeight: "18px",
          }}
        >
          {error}
        </div>
      )}

      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          gap: 10,
          marginBottom: 8,
        }}
      >
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: 11,
            fontWeight: 600,
            color: "var(--quartz-secondary)",
            background: "rgba(69, 97, 123, 0.08)",
            border: "1px solid rgba(69, 97, 123, 0.24)",
            borderRadius: 999,
            padding: "4px 9px",
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: "var(--quartz-secondary)",
            }}
          />
          Context-aware agent
        </div>
        <p
          style={{
            margin: 0,
            color: "var(--quartz-muted)",
            fontSize: 11,
            lineHeight: "17px",
          }}
        >
          Questions, workflow actions, approvals, and attachments all use one message lane. The
          agent decides when to answer directly and when to stage or apply a tool.
        </p>
      </div>

      {!disabled &&
      inputValue.trim().length === 0 &&
      attachments.length === 0 &&
      starterPrompts.length > 0 ? (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 8,
            marginBottom: 10,
          }}
        >
          {starterPrompts.map((prompt) => (
            <button
              key={prompt}
              onClick={() => {
                setInputValue(prompt);
                setError(null);
              }}
              style={{
                background: "var(--quartz-surface)",
                border: "1px solid var(--quartz-border)",
                borderRadius: 999,
                color: "var(--quartz-muted)",
                cursor: "pointer",
                fontSize: 11,
                lineHeight: "16px",
                padding: "5px 10px",
              }}
              type="button"
            >
              {prompt}
            </button>
          ))}
        </div>
      ) : null}

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
              background: "rgba(69, 97, 123, 0.1)",
              border: "1px solid rgba(69, 97, 123, 0.24)",
              borderRadius: 8,
              color: "var(--quartz-secondary)",
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
            }}
            ref={fileInputRef}
            style={{ display: "none" }}
            type="file"
          />
          {attachments.length > 0 ? (
            <select
              onChange={(event) => setAttachmentIntent(event.target.value as ChatAttachmentIntent)}
              style={{
                background: "var(--quartz-surface)",
                border: "1px solid var(--quartz-border)",
                borderRadius: 8,
                color: "var(--quartz-ink)",
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
              color: "var(--quartz-muted)",
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
                background: "var(--quartz-surface)",
                border: "1px solid var(--quartz-border)",
                borderRadius: 999,
                color: "var(--quartz-ink)",
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
              <span style={{ color: "var(--quartz-muted)", fontSize: 10 }}>
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
              attachments.length > 0
                ? "Tell the agent what to do with the attached files..."
                : "Ask normally, e.g. 'I need the reports', request a workflow step, approve work, or tell the agent what to change..."
            }
            disabled={isSubmitting}
            rows={1}
            style={{
              flex: 1,
              fontSize: 13,
              lineHeight: "20px",
              color: "var(--quartz-ink)",
              background: "var(--quartz-surface)",
              border: "1px solid var(--quartz-border)",
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
              background:
                hasInput && !isSubmitting ? "var(--quartz-primary)" : "var(--quartz-surface-high)",
              color:
                hasInput && !isSubmitting
                  ? "var(--quartz-primary-contrast)"
                  : "var(--quartz-muted)",
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

function buildStarterPrompts(options: {
  closeRunId: string | undefined;
  workspace: ChatThreadWorkspace | null;
}): string[] {
  const prompts: string[] = [];
  const { closeRunId, workspace } = options;
  const activePhase = workspace?.readiness.phase_states.find(
    (phaseState) => phaseState.status !== "completed",
  );

  if (workspace?.readiness.blockers.length) {
    prompts.push("What's blocking this run right now?");
  }
  if ((workspace?.memory.pending_action_count ?? 0) > 0) {
    prompts.push("What approvals are pending?");
  }
  if (closeRunId && (workspace?.readiness.document_count ?? 0) > 0) {
    prompts.push("I uploaded the wrong document by mistake.");
  }
  if (closeRunId && activePhase && activePhase.phase !== "collection") {
    prompts.push("Take this back one step.");
    prompts.push("Take this back to Collection so I can upload more files.");
  }
  if ((workspace?.readiness.next_actions.length ?? 0) > 0) {
    prompts.push("Continue from where we left off.");
  }
  if (closeRunId) {
    prompts.push("I need the reports.");
    prompts.push("What can you do next from here?");
  } else {
    prompts.push("Start a new run for this month.");
    prompts.push("What can you do in this workspace?");
  }

  return Array.from(new Set(prompts)).slice(0, 5);
}
