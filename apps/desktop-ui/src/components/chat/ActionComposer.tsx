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
import { QuartzIcon } from "../layout/QuartzIcons";

export type ComposerDraft = {
  attachmentNames: readonly string[];
  content: string;
  hasAttachments: boolean;
};

export type ActionComposerProps = {
  assistantMode?: "close_run" | "entity" | "global";
  closeRunId?: string | undefined;
  disabled?: boolean;
  entityId: string;
  onActionStateChange?: (action: ChatActionSummary) => void;
  onMessageSent: (response: ChatActionResponse, draft: ComposerDraft) => void;
  onSubmissionError?: (message: string) => void;
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

const PENDING_TOOL_LABELS: Record<string, string> = {
  approve_close_run: "Approve close",
  archive_close_run: "Archive close",
  delete_close_run: "Delete close",
  delete_workspace: "Delete workspace",
  distribute_export: "Distribute export",
};

const AUTO_RELEASE_TOOLS = new Set(["approve_close_run", "archive_close_run", "distribute_export"]);

export function ActionComposer({
  assistantMode,
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
  const [autoApproveRelease, setAutoApproveRelease] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const resolvedAssistantMode = assistantMode ?? (closeRunId ? "close_run" : "entity");
  const hasCloseRunScope = typeof closeRunId === "string" && closeRunId.trim().length > 0;
  const allowAttachments = hasCloseRunScope;

  const starterPrompts = useMemo(
    () => buildStarterPrompts({ assistantMode: resolvedAssistantMode, closeRunId, workspace }),
    [closeRunId, resolvedAssistantMode, workspace],
  );
  const composerPlaceholder = useMemo(
    () =>
      buildComposerPlaceholder({
        allowAttachments,
        attachmentCount: attachments.length,
        assistantMode: resolvedAssistantMode,
        starterPrompts,
      }),
    [allowAttachments, attachments.length, resolvedAssistantMode, starterPrompts],
  );

  const resetComposer = useCallback(() => {
    setInputValue("");
    setAttachments([]);
    setError(null);
    setIsLoading(false);
    if (fileInputRef.current !== null) {
      fileInputRef.current.value = "";
    }
  }, []);

  const loadPendingActions = useCallback(async () => {
    if (threadId.trim().length === 0) {
      setPendingActions([]);
      return;
    }

    try {
      const actions = await listThreadActions(threadId, entityId);
      setPendingActions(actions);
    } catch (caughtError: unknown) {
      if (caughtError instanceof ChatApiError && caughtError.status !== 404) {
        console.warn("Failed to load pending chat actions:", caughtError);
      }
    }
  }, [entityId, threadId]);

  useEffect(() => {
    resetComposer();
    void loadPendingActions();
  }, [loadPendingActions, resetComposer, threadId]);

  useEffect(() => {
    const textarea = textareaRef.current;
    if (textarea === null) {
      return;
    }
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 156)}px`;
  }, [inputValue]);

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
      if (attachments.length > 0 && !allowAttachments) {
        setError("Open a close run before uploading source documents in chat.");
        return;
      }

      const draft: ComposerDraft = {
        attachmentNames: allowAttachments ? attachments.map((file) => file.name) : [],
        content:
          trimmed.length > 0
            ? trimmed
            : attachments.length === 1
              ? "Uploaded 1 source document."
              : `Uploaded ${attachments.length} source documents.`,
        hasAttachments: allowAttachments && attachments.length > 0,
      };

      setIsLoading(true);
      setError(null);
      onSubmissionStart?.(draft);
      const clientTurnId = buildClientTurnId();

      try {
        const actionResponse =
          allowAttachments && attachments.length > 0
            ? await sendChatActionWithAttachments(
              threadId,
              entityId,
              trimmed.length > 0
                ? {
                  attachmentIntent: "source_documents",
                  clientTurnId,
                  content: trimmed,
                  files: attachments,
                }
                : {
                  attachmentIntent: "source_documents",
                  clientTurnId,
                  files: attachments,
                },
            )
            : await sendChatAction(threadId, entityId, trimmed, clientTurnId);

        resetComposer();
        onMessageSent(actionResponse, draft);
        await loadPendingActions();
      } catch (caughtError: unknown) {
        if (
          caughtError instanceof ChatApiError &&
          caughtError.status === 504 &&
          !(allowAttachments && attachments.length > 0)
        ) {
          try {
            const replayedResponse = await sendChatAction(threadId, entityId, trimmed, clientTurnId);
            resetComposer();
            onMessageSent(replayedResponse, draft);
            await loadPendingActions();
            return;
          } catch (retryError: unknown) {
            caughtError = retryError;
          }
        }
        const failureMessage = buildSubmissionFailureMessage(caughtError);
        if (onSubmissionError) {
          setError(null);
          onSubmissionError(failureMessage);
        } else {
          setError(failureMessage);
        }
      } finally {
        setIsLoading(false);
      }
    },
    [
      attachments,
      allowAttachments,
      disabled,
      entityId,
      inputValue,
      isLoading,
      loadPendingActions,
      onMessageSent,
      onSubmissionError,
      onSubmissionStart,
      resetComposer,
      threadId,
    ],
  );

  const handleActionApproval = useCallback(
    async (action: ChatActionSummary, continueAfterApproval: boolean) => {
      const actionId = action.id;
      if (loadingActions.has(actionId) || threadId.trim().length === 0) {
        return;
      }

      setLoadingActions((current) => new Set(current).add(actionId));
      try {
        const shouldAutoApproveRelease =
          autoApproveRelease && isAutoReleaseEligibleAction(action);
        const updated = await approveChatAction(
          actionId,
          threadId,
          entityId,
          shouldAutoApproveRelease
            ? {
              approvalPolicy: "auto_release_for_thread",
              reason: "Approved from chat; auto-approve release controls for this thread.",
            }
            : {
              reason: "Approved from chat.",
            },
        );
        setPendingActions((current) => current.filter((action) => action.id !== actionId));
        onActionStateChange?.(updated);
        if (continueAfterApproval) {
          const prompt = buildApprovalContinuationPrompt({
            action,
            autoApproveRelease,
            wasApproved: true,
          });
          const response = await sendChatAction(threadId, entityId, prompt, buildClientTurnId());
          onMessageSent(response, {
            attachmentNames: [],
            content: prompt,
            hasAttachments: false,
          });
          await loadPendingActions();
        }
      } catch (caughtError: unknown) {
        setError(
          caughtError instanceof ChatApiError
            ? caughtError.message
            : "I couldn't approve that action because the request stopped before the assistant received a normal response.",
        );
      } finally {
        setLoadingActions((current) => {
          const next = new Set(current);
          next.delete(actionId);
          return next;
        });
      }
    },
    [
      autoApproveRelease,
      entityId,
      loadPendingActions,
      loadingActions,
      onActionStateChange,
      onMessageSent,
      threadId,
    ],
  );

  const handleActionRejection = useCallback(
    async (action: ChatActionSummary, continueAfterSkip: boolean) => {
      const actionId = action.id;
      if (loadingActions.has(actionId) || threadId.trim().length === 0) {
        return;
      }

      setLoadingActions((current) => new Set(current).add(actionId));
      try {
        const updated = await rejectChatAction(
          actionId,
          threadId,
          entityId,
          continueAfterSkip
            ? "Skipped from the chat approval prompt; continue the remaining workflow."
            : "Skipped from the chat approval prompt.",
        );
        setPendingActions((current) => current.filter((action) => action.id !== actionId));
        onActionStateChange?.(updated);
        if (continueAfterSkip) {
          const prompt = buildApprovalContinuationPrompt({
            action,
            autoApproveRelease: false,
            wasApproved: false,
          });
          const response = await sendChatAction(threadId, entityId, prompt, buildClientTurnId());
          onMessageSent(response, {
            attachmentNames: [],
            content: prompt,
            hasAttachments: false,
          });
          await loadPendingActions();
        }
      } catch (caughtError: unknown) {
        setError(
          caughtError instanceof ChatApiError
            ? caughtError.message
            : "I couldn't reject that action because the request stopped before the assistant received a normal response.",
        );
      } finally {
        setLoadingActions((current) => {
          const next = new Set(current);
          next.delete(actionId);
          return next;
        });
      }
    },
    [entityId, loadPendingActions, loadingActions, onActionStateChange, onMessageSent, threadId],
  );

  const hasInput = inputValue.trim().length > 0 || attachments.length > 0;
  const isSubmitting = isLoading || disabled;

  return (
    <div style={composerContainerStyle}>
      {pendingActions.length > 0 ? (
        <div style={pendingActionListStyle}>
          {pendingActions.map((action) => {
            const isBusy = loadingActions.has(action.id);
            const title = formatPendingActionTitle(action);
            const showAutoRelease = isAutoReleaseEligibleAction(action);
            return (
              <div key={action.id} style={pendingActionCardStyle}>
                <div style={pendingActionHeaderStyle}>
                  <span style={pendingActionLabelStyle}>{title}</span>
                  <span style={pendingActionBadgeStyle}>Review</span>
                </div>

                {action.assistant_response ? (
                  <p style={pendingActionDescriptionStyle}>{action.assistant_response}</p>
                ) : null}

                {showAutoRelease ? (
                  <label style={pendingActionToggleStyle}>
                    <input
                      checked={autoApproveRelease}
                      disabled={isBusy}
                      onChange={(event) => {
                        setAutoApproveRelease(event.currentTarget.checked);
                      }}
                      type="checkbox"
                    />
                    <span>Auto-release (no deletes)</span>
                  </label>
                ) : null}

                <div style={pendingActionButtonRowStyle}>
                  <button
                    disabled={isBusy}
                    onClick={() => {
                      void handleActionApproval(action, false);
                    }}
                    style={pendingApproveButtonStyle(isBusy)}
                    type="button"
                  >
                    {isBusy ? "Saving..." : "Approve"}
                  </button>
                  <button
                    disabled={isBusy}
                    onClick={() => {
                      void handleActionApproval(action, true);
                    }}
                    style={pendingPrimaryButtonStyle(isBusy)}
                    type="button"
                  >
                    {isBusy ? "Saving..." : "Approve & continue"}
                  </button>
                  <button
                    disabled={isBusy}
                    onClick={() => {
                      void handleActionRejection(action, false);
                    }}
                    style={pendingRejectButtonStyle(isBusy)}
                    type="button"
                  >
                    {isBusy ? "Saving..." : "Skip"}
                  </button>
                  <button
                    disabled={isBusy}
                    onClick={() => {
                      void handleActionRejection(action, true);
                    }}
                    style={pendingSecondaryButtonStyle(isBusy)}
                    type="button"
                  >
                    {isBusy ? "Saving..." : "Skip & continue"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {error ? (
        <div role="status" style={errorBannerStyle}>
          {error}
        </div>
      ) : null}

      <form
        onSubmit={(event) => {
          void handleSubmit(event);
        }}
        style={composerFormStyle}
      >
        <div style={composerShellStyle}>
          <div style={composerInputRowStyle}>
            <>
              <button
                aria-label="Upload document"
                onClick={() => {
                  if (!allowAttachments) {
                    setError("Open a close run before uploading source documents in chat.");
                    return;
                  }
                  fileInputRef.current?.click();
                }}
                style={attachmentButtonStyle}
                title={
                  allowAttachments
                    ? "Upload document"
                    : "Open a close run before uploading source documents"
                }
                type="button"
              >
                <QuartzIcon name="upload" style={composerButtonIconStyle} />
              </button>
              <input
                accept=".pdf,.csv,.xlsx,.xls,.xlsm"
                disabled={!allowAttachments}
                multiple
                onChange={(event) => {
                  if (!allowAttachments) {
                    setAttachments([]);
                    setError("Open a close run before uploading source documents in chat.");
                    return;
                  }
                  setAttachments(Array.from(event.target.files ?? []));
                  setError(null);
                }}
                ref={fileInputRef}
                style={{ display: "none" }}
                type="file"
              />
            </>

            <textarea
              disabled={isSubmitting}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
                if (event.key === "Enter" && !event.shiftKey && !event.metaKey && !event.ctrlKey) {
                  event.preventDefault();
                  if (hasInput && !isSubmitting) {
                    void handleSubmit(event);
                  }
                }
              }}
              placeholder={composerPlaceholder}
              ref={textareaRef}
              rows={1}
              style={composerTextareaStyle}
              value={inputValue}
            />

            <button
              aria-label={isSubmitting ? "Sending message" : "Send message"}
              disabled={!hasInput || isSubmitting}
              style={sendButtonStyle(!hasInput || isSubmitting)}
              title={isSubmitting ? "Sending" : "Send"}
              type="submit"
            >
              {isSubmitting ? (
                <span style={sendButtonTextStyle}>Sending</span>
              ) : (
                <QuartzIcon name="send" style={sendIconStyle} />
              )}
            </button>
          </div>

          {attachments.length > 0 ? (
            <div style={attachmentBarStyle}>
              <div style={attachmentListStyle}>
                {attachments.map((file) => (
                  <span key={`${file.name}:${file.size}`} style={attachmentTokenStyle}>
                    <span style={attachmentTokenNameStyle}>{file.name}</span>
                    <span style={attachmentTokenMetaStyle}>{formatByteSize(file.size)}</span>
                  </span>
                ))}
              </div>
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
                Clear
              </button>
            </div>
          ) : null}
        </div>
      </form>
    </div>
  );
}

function buildClientTurnId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function buildApprovalContinuationPrompt(input: {
  action: ChatActionSummary;
  autoApproveRelease: boolean;
  wasApproved: boolean;
}): string {
  const targetLabel =
    input.action.tool_name !== null
      ? formatPendingActionTitle(input.action)
      : input.action.target_type !== null
        ? `${input.action.target_type}${input.action.target_id ? ` ${input.action.target_id}` : ""}`
        : input.action.intent.replaceAll("_", " ");
  if (input.wasApproved) {
    return input.autoApproveRelease
      ? `I approved the pending ${targetLabel}. Continue the same workflow, and apply the thread's auto-release approval policy to future non-destructive release steps.`
      : `I approved the pending ${targetLabel}. Continue the same workflow from the updated state.`;
  }
  return `I skipped the pending ${targetLabel}. Continue the same workflow without applying that skipped step, and summarize anything that is now blocked or not applicable.`;
}

function formatPendingActionTitle(action: ChatActionSummary): string {
  if (action.tool_name !== null) {
    return PENDING_TOOL_LABELS[action.tool_name] ?? titleCase(action.tool_name.replaceAll("_", " "));
  }
  return ACTION_INTENT_LABELS[action.intent] ?? titleCase(action.intent.replaceAll("_", " "));
}

function isAutoReleaseEligibleAction(action: ChatActionSummary): boolean {
  return action.tool_name !== null && AUTO_RELEASE_TOOLS.has(action.tool_name);
}

function titleCase(value: string): string {
  return value
    .split(" ")
    .filter(Boolean)
    .map((word) => `${word.slice(0, 1).toUpperCase()}${word.slice(1)}`)
    .join(" ");
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

function buildSubmissionFailureMessage(caughtError: unknown): string {
  if (caughtError instanceof ChatApiError) {
    if (caughtError.status === 401) {
      return "I couldn't continue because your session is no longer authenticated. Sign in again, then send the request once more.";
    }
    if (caughtError.status === 403) {
      return `I couldn't access the workspace or record needed for that turn. ${caughtError.message}`;
    }
    if (caughtError.status === 404) {
      return `I couldn't find the selected chat or workspace context. ${caughtError.message}`;
    }
    if (caughtError.status === 504) {
      return "The chat request took too long to return its final state. I retried the same turn key, so refresh the thread and I will continue from the latest confirmed workspace state.";
    }
    if (caughtError.status >= 500) {
      return `I hit a backend system error while handling that turn. I did not apply further changes. ${caughtError.message}`;
    }
    return caughtError.message;
  }

  return "I couldn't finish that turn because the chat request failed before the assistant could receive a normal response. I did not apply further changes.";
}

function buildStarterPrompts(options: {
  assistantMode: "close_run" | "entity" | "global";
  closeRunId: string | undefined;
  workspace: ChatThreadWorkspace | null;
}): string[] {
  const prompts: string[] = [];
  const { assistantMode, closeRunId, workspace } = options;
  const activePhase = workspace?.readiness.phase_states.find(
    (phaseState) => phaseState.status !== "completed",
  );

  if (assistantMode === "global") {
    prompts.push("Show me the workspaces that need attention.");
    prompts.push("Which workspace should I work on first?");
    prompts.push("Switch this chat to another workspace.");
    prompts.push("Summarize the active close runs across my workspaces.");
    prompts.push("Create a new workspace.");
    prompts.push("Which workspace has blockers right now?");
    return Array.from(new Set(prompts)).slice(0, 5);
  }

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
  } else {
    prompts.push("Show me the active close runs in this workspace.");
    prompts.push("Start a new close run for this entity.");
    prompts.push("What workspace data is missing before the next close?");
  }

  return Array.from(new Set(prompts)).slice(0, 5);
}

function buildComposerPlaceholder(options: {
  allowAttachments: boolean;
  assistantMode: "close_run" | "entity" | "global";
  attachmentCount: number;
  starterPrompts: readonly string[];
}): string {
  if (options.attachmentCount > 0 && options.allowAttachments) {
    return "Tell the assistant what to do with these documents...";
  }
  if (options.starterPrompts[0]) {
    return options.starterPrompts[0];
  }
  if (options.assistantMode === "global") {
    return "Ask across workspaces or choose where to work next...";
  }
  if (options.assistantMode === "entity") {
    return "Ask about this workspace or what to do next...";
  }
  return "Ask about the close or request the next step...";
}

const composerContainerStyle = {
  borderTop: "1px solid var(--quartz-border)",
  background: "linear-gradient(180deg, rgba(247, 243, 242, 0.9) 0%, rgba(253, 248, 248, 1) 100%)",
  display: "grid",
  gap: 8,
  padding: "8px 20px 10px",
} satisfies React.CSSProperties;

const pendingActionListStyle = {
  display: "grid",
  gap: 8,
  margin: "0 auto",
  maxWidth: 1080,
  paddingBottom: 2,
  width: "100%",
} satisfies React.CSSProperties;

const pendingActionCardStyle = {
  alignItems: "center",
  border: "1px solid rgba(142, 115, 75, 0.36)",
  background: "linear-gradient(90deg, rgba(255, 251, 235, 0.9) 0%, rgba(255, 255, 255, 0.86) 100%)",
  borderRadius: 10,
  display: "grid",
  gap: 14,
  gridTemplateColumns: "minmax(0, 1fr) auto",
  minWidth: 0,
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
  border: "1px solid rgba(142, 115, 75, 0.18)",
  borderRadius: 999,
  color: "var(--quartz-gold)",
  fontSize: 11,
  fontWeight: 600,
  padding: "4px 8px",
} satisfies React.CSSProperties;

const pendingActionDescriptionStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
  lineHeight: "17px",
  margin: 0,
} satisfies React.CSSProperties;

const pendingActionToggleStyle = {
  alignItems: "center",
  color: "var(--quartz-muted)",
  display: "flex",
  fontSize: 12,
  gap: 8,
  lineHeight: "16px",
} satisfies React.CSSProperties;

const pendingActionButtonRowStyle = {
  display: "grid",
  gap: 8,
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
  minWidth: 220,
} satisfies React.CSSProperties;

function pendingPrimaryButtonStyle(disabled: boolean) {
  return {
    border: "1px solid rgba(27, 67, 50, 0.2)",
    borderRadius: 10,
    background: disabled ? "rgba(27, 67, 50, 0.08)" : "var(--quartz-success)",
    color: disabled ? "var(--quartz-success)" : "white",
    cursor: disabled ? "not-allowed" : "pointer",
    fontSize: 12,
    fontWeight: 700,
    minHeight: 34,
    opacity: disabled ? 0.6 : 1,
  } satisfies React.CSSProperties;
}

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

function pendingSecondaryButtonStyle(disabled: boolean) {
  return {
    border: "1px solid rgba(142, 115, 75, 0.22)",
    borderRadius: 10,
    background: "rgba(255, 251, 235, 0.92)",
    color: "var(--quartz-gold)",
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
  border: "1px solid rgba(123, 45, 38, 0.22)",
  borderRadius: 12,
  background: "rgba(255, 218, 214, 0.72)",
  color: "var(--quartz-error)",
  fontSize: 12,
  lineHeight: "18px",
  padding: "10px 12px",
} satisfies React.CSSProperties;

const composerFormStyle = {
  display: "grid",
} satisfies React.CSSProperties;

const composerShellStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 12,
  background: "rgba(255, 255, 255, 0.92)",
  boxShadow: "0 8px 24px rgba(28, 27, 27, 0.06)",
  display: "grid",
  gap: 8,
  margin: "0 auto",
  maxWidth: 1080,
  padding: "8px 10px",
  width: "100%",
} satisfies React.CSSProperties;

const composerInputRowStyle = {
  alignItems: "end",
  display: "grid",
  gap: 8,
  gridTemplateColumns: "auto minmax(0, 1fr) auto",
} satisfies React.CSSProperties;

const composerTextareaStyle = {
  width: "100%",
  minHeight: 34,
  maxHeight: 140,
  border: "none",
  borderRadius: 8,
  background: "rgba(252, 252, 250, 0.72)",
  color: "var(--quartz-ink)",
  fontFamily: "inherit",
  fontSize: 14,
  lineHeight: "20px",
  outline: "none",
  overflowY: "auto",
  padding: "7px 10px",
  resize: "none",
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

const attachmentBarStyle = {
  alignItems: "center",
  display: "flex",
  gap: 8,
  justifyContent: "space-between",
} satisfies React.CSSProperties;

const attachmentButtonStyle = {
  alignItems: "center",
  alignSelf: "end",
  border: "none",
  borderRadius: 999,
  background: "transparent",
  color: "var(--quartz-muted)",
  cursor: "pointer",
  display: "inline-flex",
  flex: "0 0 auto",
  height: 34,
  justifyContent: "center",
  padding: 0,
  transition: "color 120ms ease",
  width: 34,
} satisfies React.CSSProperties;

const composerButtonIconStyle = {
  height: 16,
  width: 16,
} satisfies React.CSSProperties;

const clearButtonStyle = {
  border: "none",
  background: "transparent",
  color: "var(--quartz-error)",
  cursor: "pointer",
  flex: "0 0 auto",
  fontSize: 11,
  fontWeight: 600,
  padding: "2px 4px",
} satisfies React.CSSProperties;

function sendButtonStyle(disabled: boolean) {
  return {
    border: "none",
    borderRadius: 8,
    background: disabled ? "var(--quartz-surface-high)" : "var(--quartz-primary)",
    color: disabled ? "var(--quartz-muted)" : "var(--quartz-primary-contrast)",
    cursor: disabled ? "not-allowed" : "pointer",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    fontSize: 12,
    fontWeight: 700,
    height: 34,
    minWidth: disabled ? 64 : 36,
    padding: disabled ? "0 10px" : 0,
    transition: "background 120ms ease",
  } satisfies React.CSSProperties;
}

const sendIconStyle = {
  height: 15,
  width: 15,
} satisfies React.CSSProperties;

const sendButtonTextStyle = {
  fontSize: 12,
} satisfies React.CSSProperties;
