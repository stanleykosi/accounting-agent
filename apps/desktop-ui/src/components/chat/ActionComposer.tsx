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
import type * as React from "react";
import {
  approveChatAction,
  type ChatActionResponse,
  type ChatActionSummary,
  type ChatThreadWorkspace,
  ChatApiError,
  listThreadActions,
  rejectChatAction,
  sendChatAction,
  sendChatActionWithAttachments,
} from "../../lib/chat";

export type ComposerDraft = {
  attachmentNames: readonly string[];
  content: string;
  hasAttachments: boolean;
};

export type ActionComposerProps = {
  closeRunId?: string | undefined;
  disabled?: boolean;
  entityId: string;
  onActionStateChange?: (action: ChatActionSummary) => void;
  onMessageSent: (response: ChatActionResponse, draft: ComposerDraft) => void;
  onSubmissionError?: () => void;
  onSubmissionStart?: (draft: ComposerDraft) => void;
  threadId: string;
  workspace?: ChatThreadWorkspace | null;
};

const ACTION_INTENT_LABELS: Record<string, string> = {
  approval_request: "Approval",
  document_request: "Document request",
  explanation: "Explanation",
  proposed_edit: "Proposed change",
  reconciliation_query: "Reconciliation",
  report_action: "Reporting",
  workflow_action: "Workflow",
};

export function ActionComposer({
  closeRunId,
  disabled = false,
  entityId,
  onActionStateChange,
  onMessageSent,
  onSubmissionError,
  onSubmissionStart,
  threadId,
  workspace = null,
}: Readonly<ActionComposerProps>) {
  const [inputValue, setInputValue] = useState("");
  const [attachments, setAttachments] = useState<readonly File[]>([]);
  const [pendingActions, setPendingActions] = useState<ChatActionSummary[]>([]);
  const [loadingActions, setLoadingActions] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

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
    } catch (error: unknown) {
      if (error instanceof ChatApiError && error.status !== 404) {
        console.warn("Failed to load pending chat actions:", error);
      }
    }
  }, [entityId, threadId]);

  useEffect(() => {
    void loadPendingActions();
  }, [loadPendingActions]);

  const handleSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const trimmed = inputValue.trim();
      if ((trimmed.length === 0 && attachments.length === 0) || isLoading || disabled) {
        return;
      }
      if (threadId.trim().length === 0) {
        setError("Open a chat before sending a message.");
        return;
      }

      const draft: ComposerDraft = {
        attachmentNames: attachments.map((file) => file.name),
        content:
          trimmed.length > 0
            ? trimmed
            : attachments.length === 1
              ? "Uploaded 1 source document."
              : `Uploaded ${attachments.length} source documents.`,
        hasAttachments: attachments.length > 0,
      };

      setIsLoading(true);
      setError(null);
      onSubmissionStart?.(draft);

      try {
        const actionResponse =
          attachments.length > 0
            ? await sendChatActionWithAttachments(
                threadId,
                entityId,
                trimmed.length > 0
                  ? {
                      attachmentIntent: "source_documents",
                      content: trimmed,
                      files: attachments,
                    }
                  : {
                      attachmentIntent: "source_documents",
                      files: attachments,
                    },
              )
            : await sendChatAction(threadId, entityId, trimmed);

        setInputValue("");
        setAttachments([]);
        if (fileInputRef.current !== null) {
          fileInputRef.current.value = "";
        }
        onMessageSent(actionResponse, draft);
        await loadPendingActions();
      } catch (error: unknown) {
        setError(
          error instanceof ChatApiError
            ? error.message
            : "The assistant could not complete this turn. Try again.",
        );
        onSubmissionError?.();
      } finally {
        setIsLoading(false);
      }
    },
    [
      attachments,
      disabled,
      entityId,
      inputValue,
      isLoading,
      loadPendingActions,
      onMessageSent,
      onSubmissionError,
      onSubmissionStart,
      threadId,
    ],
  );

  const handleActionApproval = useCallback(
    async (actionId: string) => {
      if (loadingActions.has(actionId) || threadId.trim().length === 0) {
        return;
      }

      setLoadingActions((current) => new Set(current).add(actionId));
      try {
        const updated = await approveChatAction(actionId, threadId, entityId);
        setPendingActions((current) => current.filter((action) => action.id !== actionId));
        onActionStateChange?.(updated);
      } catch (error: unknown) {
        setError(
          error instanceof ChatApiError
            ? error.message
            : "The approval could not be completed. Try again.",
        );
      } finally {
        setLoadingActions((current) => {
          const next = new Set(current);
          next.delete(actionId);
          return next;
        });
      }
    },
    [entityId, loadingActions, onActionStateChange, threadId],
  );

  const handleActionRejection = useCallback(
    async (actionId: string) => {
      if (loadingActions.has(actionId) || threadId.trim().length === 0) {
        return;
      }

      setLoadingActions((current) => new Set(current).add(actionId));
      try {
        const updated = await rejectChatAction(
          actionId,
          threadId,
          entityId,
          "Rejected from the assistant workspace.",
        );
        setPendingActions((current) => current.filter((action) => action.id !== actionId));
        onActionStateChange?.(updated);
      } catch (error: unknown) {
        setError(
          error instanceof ChatApiError
            ? error.message
            : "The rejection could not be completed. Try again.",
        );
      } finally {
        setLoadingActions((current) => {
          const next = new Set(current);
          next.delete(actionId);
          return next;
        });
      }
    },
    [entityId, loadingActions, onActionStateChange, threadId],
  );

  const hasInput = inputValue.trim().length > 0 || attachments.length > 0;
  const isSubmitting = isLoading || disabled;

  return (
    <div
      style={{
        borderTop: "1px solid var(--quartz-border)",
        background:
          "linear-gradient(180deg, rgba(247, 243, 242, 0.94) 0%, rgba(253, 248, 248, 1) 100%)",
        padding: "18px 24px 22px",
      }}
    >
      {pendingActions.length > 0 ? (
        <div style={pendingActionListStyle}>
          {pendingActions.map((action) => {
            const isBusy = loadingActions.has(action.id);
            return (
              <div key={action.id} style={pendingActionCardStyle}>
                <div style={pendingActionHeaderStyle}>
                  <span style={pendingActionLabelStyle}>
                    {ACTION_INTENT_LABELS[action.intent] ?? action.intent.replaceAll("_", " ")}
                  </span>
                  <span style={pendingActionBadgeStyle}>Review required</span>
                </div>
                <div style={pendingActionButtonRowStyle}>
                  <button
                    disabled={isBusy}
                    onClick={() => {
                      void handleActionApproval(action.id);
                    }}
                    style={pendingApproveButtonStyle(isBusy)}
                    type="button"
                  >
                    {isBusy ? "..." : "Approve"}
                  </button>
                  <button
                    disabled={isBusy}
                    onClick={() => {
                      void handleActionRejection(action.id);
                    }}
                    style={pendingRejectButtonStyle(isBusy)}
                    type="button"
                  >
                    {isBusy ? "..." : "Reject"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {error ? (
        <div style={errorBannerStyle} role="status">
          {error}
        </div>
      ) : null}

      {!disabled && inputValue.trim().length === 0 && attachments.length === 0 && starterPrompts.length > 0 ? (
        <div style={suggestionRowStyle}>
          {starterPrompts.map((prompt) => (
            <button
              key={prompt}
              onClick={() => {
                setInputValue(prompt);
                setError(null);
              }}
              style={suggestionChipStyle}
              type="button"
            >
              {prompt}
            </button>
          ))}
        </div>
      ) : null}

      <form
        onSubmit={(event) => {
          void handleSubmit(event);
        }}
        style={composerFormStyle}
      >
        <div style={composerShellStyle}>
          <textarea
            disabled={isSubmitting}
            onChange={(event) => setInputValue(event.target.value)}
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
                ? "Tell the assistant what to do with these source documents..."
                : "Ask about the close, request the next step, or upload source documents..."
            }
            rows={1}
            style={composerTextareaStyle}
            value={inputValue}
          />

          {attachments.length > 0 ? (
            <div style={attachmentListStyle}>
              {attachments.map((file) => (
                <span key={`${file.name}:${file.size}`} style={attachmentTokenStyle}>
                  <span style={attachmentTokenNameStyle}>{file.name}</span>
                  <span style={attachmentTokenMetaStyle}>{formatByteSize(file.size)}</span>
                </span>
              ))}
            </div>
          ) : null}

          <div style={composerFooterStyle}>
            <div style={composerUtilityRowStyle}>
              <button
                onClick={() => fileInputRef.current?.click()}
                style={attachmentButtonStyle}
                type="button"
              >
                Upload source documents
              </button>
              <input
                accept=".pdf,.csv,.xlsx,.xls,.xlsm"
                multiple
                onChange={(event) => {
                  setAttachments(Array.from(event.target.files ?? []));
                  setError(null);
                }}
                ref={fileInputRef}
                style={{ display: "none" }}
                type="file"
              />
              {attachments.length > 0 ? (
                <button
                  onClick={() => {
                    setAttachments([]);
                    setError(null);
                    if (fileInputRef.current !== null) {
                      fileInputRef.current.value = "";
                    }
                  }}
                  style={clearButtonStyle}
                  type="button"
                >
                  Clear files
                </button>
              ) : null}
            </div>

            <button
              disabled={!hasInput || isSubmitting}
              style={sendButtonStyle(!hasInput || isSubmitting)}
              type="submit"
            >
              {isSubmitting ? "..." : "Send"}
            </button>
          </div>
        </div>

        <p style={composerNoteStyle}>
          Chat uploads are treated as source documents only. Use Entity Home for COA, general
          ledger, or trial balance uploads.
        </p>
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
    prompts.push("What is blocking this close right now?");
  }
  if ((workspace?.memory.pending_action_count ?? 0) > 0) {
    prompts.push("Show me the approvals waiting for review.");
  }
  if (closeRunId && (workspace?.readiness.document_count ?? 0) > 0) {
    prompts.push("Which uploaded documents still need attention?");
  }
  if (closeRunId && activePhase && activePhase.phase !== "collection") {
    prompts.push("Can you move this run back to collection?");
  }
  if ((workspace?.readiness.next_actions.length ?? 0) > 0) {
    prompts.push("Continue from where we left off.");
  }
  if (closeRunId) {
    prompts.push("What should I do next in this close?");
    prompts.push("Summarize this close for me.");
  }

  return Array.from(new Set(prompts)).slice(0, 5);
}

const pendingActionListStyle = {
  display: "grid",
  gap: 10,
  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
  marginBottom: 14,
} satisfies React.CSSProperties;

const pendingActionCardStyle = {
  border: "1px solid rgba(142, 115, 75, 0.2)",
  background: "rgba(255, 251, 235, 0.82)",
  borderRadius: 16,
  display: "grid",
  gap: 10,
  padding: "12px 14px",
} satisfies React.CSSProperties;

const pendingActionHeaderStyle = {
  alignItems: "center",
  display: "flex",
  gap: 8,
  justifyContent: "space-between",
} satisfies React.CSSProperties;

const pendingActionLabelStyle = {
  color: "var(--quartz-ink)",
  fontSize: 12,
  fontWeight: 700,
  letterSpacing: "0.02em",
  textTransform: "uppercase",
} satisfies React.CSSProperties;

const pendingActionBadgeStyle = {
  border: "1px solid rgba(142, 115, 75, 0.2)",
  borderRadius: 999,
  color: "var(--quartz-gold)",
  fontSize: 11,
  fontWeight: 600,
  padding: "4px 8px",
} satisfies React.CSSProperties;

const pendingActionButtonRowStyle = {
  display: "grid",
  gap: 8,
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
} satisfies React.CSSProperties;

function pendingApproveButtonStyle(disabled: boolean) {
  return {
    border: "1px solid rgba(27, 67, 50, 0.18)",
    borderRadius: 10,
    background: "rgba(27, 67, 50, 0.08)",
    color: "var(--quartz-success)",
    cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 12,
    fontWeight: 600,
    minHeight: 34,
    opacity: disabled ? 0.6 : 1,
  } satisfies React.CSSProperties;
}

function pendingRejectButtonStyle(disabled: boolean) {
  return {
    border: "1px solid rgba(123, 45, 38, 0.22)",
    borderRadius: 10,
    background: "rgba(255, 218, 214, 0.72)",
    color: "var(--quartz-error)",
    cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 12,
    fontWeight: 600,
    minHeight: 34,
    opacity: disabled ? 0.6 : 1,
  } satisfies React.CSSProperties;
}

const errorBannerStyle = {
  marginBottom: 12,
  padding: "10px 12px",
  borderRadius: 12,
  background: "rgba(255, 218, 214, 0.72)",
  border: "1px solid rgba(123, 45, 38, 0.22)",
  color: "var(--quartz-error)",
  fontSize: 12,
  lineHeight: "18px",
} satisfies React.CSSProperties;

const suggestionRowStyle = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  marginBottom: 12,
} satisfies React.CSSProperties;

const suggestionChipStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  background: "rgba(255, 255, 255, 0.74)",
  color: "var(--quartz-muted)",
  cursor: "pointer",
  fontSize: 12,
  lineHeight: "18px",
  padding: "8px 12px",
} satisfies React.CSSProperties;

const composerFormStyle = {
  display: "grid",
  gap: 10,
} satisfies React.CSSProperties;

const composerShellStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 22,
  background: "var(--quartz-surface)",
  boxShadow: "0 10px 28px rgba(17, 24, 39, 0.06)",
  display: "grid",
  gap: 12,
  padding: "14px 16px 14px",
} satisfies React.CSSProperties;

const composerTextareaStyle = {
  width: "100%",
  minHeight: 44,
  maxHeight: 180,
  border: "none",
  background: "transparent",
  color: "var(--quartz-ink)",
  fontFamily: "inherit",
  fontSize: 15,
  lineHeight: "24px",
  outline: "none",
  padding: 0,
  resize: "vertical",
} satisfies React.CSSProperties;

const attachmentListStyle = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
} satisfies React.CSSProperties;

const attachmentTokenStyle = {
  alignItems: "center",
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  background: "var(--quartz-surface-low)",
  color: "var(--quartz-ink)",
  display: "inline-flex",
  gap: 8,
  maxWidth: "100%",
  padding: "7px 12px",
} satisfies React.CSSProperties;

const attachmentTokenNameStyle = {
  fontSize: 12,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} satisfies React.CSSProperties;

const attachmentTokenMetaStyle = {
  color: "var(--quartz-muted)",
  fontSize: 11,
} satisfies React.CSSProperties;

const composerFooterStyle = {
  alignItems: "center",
  display: "flex",
  gap: 12,
  justifyContent: "space-between",
} satisfies React.CSSProperties;

const composerUtilityRowStyle = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 10,
} satisfies React.CSSProperties;

const attachmentButtonStyle = {
  border: "1px solid rgba(69, 97, 123, 0.22)",
  borderRadius: 999,
  background: "rgba(69, 97, 123, 0.08)",
  color: "var(--quartz-secondary)",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 600,
  minHeight: 34,
  padding: "0 12px",
} satisfies React.CSSProperties;

const clearButtonStyle = {
  border: "none",
  background: "transparent",
  color: "var(--quartz-muted)",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 600,
  padding: 0,
} satisfies React.CSSProperties;

function sendButtonStyle(disabled: boolean) {
  return {
    border: "none",
    borderRadius: 999,
    background: disabled ? "var(--quartz-surface-high)" : "var(--quartz-primary)",
    color: disabled ? "var(--quartz-muted)" : "var(--quartz-primary-contrast)",
    cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 12,
    fontWeight: 700,
    minHeight: 38,
    minWidth: 74,
    padding: "0 16px",
  } satisfies React.CSSProperties;
}

const composerNoteStyle = {
  color: "var(--quartz-muted)",
  fontSize: 11,
  lineHeight: "17px",
  margin: 0,
  paddingLeft: 6,
} satisfies React.CSSProperties;
