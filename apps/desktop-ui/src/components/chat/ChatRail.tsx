"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../layout/QuartzIcons";
import { ActionComposer, type ComposerDraft } from "./ActionComposer";
import {
  ChatApiError,
  createChatThread,
  createGlobalChatThread,
  deleteChatThread,
  getChatThread,
  getChatThreadWorkspace,
  listChatThreads,
  listGlobalChatThreads,
  type ChatActionResponse,
  type ChatMessageRecord,
  type ChatThreadSummary,
  type ChatThreadWorkspace,
} from "../../lib/chat";

export type ChatRailProps = {
  assistantMode?: "close_run" | "entity" | "global";
  closeRunId?: string;
  entityId: string;
  presentation?: "rail" | "workspace";
};

type PendingTurn = {
  assistantContent: string | null;
  draft: ComposerDraft;
};

type RenderableMessage = ChatMessageRecord & {
  displayTime: string;
};

type CreateThreadOptions = {
  suppressError?: boolean;
};

export function ChatRail({
  assistantMode,
  closeRunId,
  entityId,
  presentation = "rail",
}: Readonly<ChatRailProps>): ReactElement {
  const resolvedAssistantMode = assistantMode ?? (closeRunId ? "close_run" : "entity");
  const isGlobalAssistant = resolvedAssistantMode === "global";
  const [activeEntityId, setActiveEntityId] = useState(entityId);
  const [threads, setThreads] = useState<ChatThreadSummary[]>([]);
  const [selectedThread, setSelectedThread] = useState<ChatThreadSummary | null>(null);
  const [messages, setMessages] = useState<ChatMessageRecord[]>([]);
  const [workspace, setWorkspace] = useState<ChatThreadWorkspace | null>(null);
  const [pendingTurn, setPendingTurn] = useState<PendingTurn | null>(null);
  const [pendingDeletionThread, setPendingDeletionThread] = useState<ChatThreadSummary | null>(
    null,
  );
  const [deletingThreadId, setDeletingThreadId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBootstrapping, setIsBootstrapping] = useState(true);
  const [isCreatingThread, setIsCreatingThread] = useState(false);
  const [isLoadingThread, setIsLoadingThread] = useState(false);
  const isCreatingThreadRef = useRef(false);
  const activeEntityIdRef = useRef(entityId);
  const selectedThreadRef = useRef<ChatThreadSummary | null>(null);
  const threadsRef = useRef<readonly ChatThreadSummary[]>([]);
  const workspaceRef = useRef<ChatThreadWorkspace | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const threadLoadRequestIdRef = useRef(0);
  const pendingTurnVersionRef = useRef(0);

  useEffect(() => {
    selectedThreadRef.current = selectedThread;
  }, [selectedThread]);

  useEffect(() => {
    activeEntityIdRef.current = activeEntityId;
  }, [activeEntityId]);

  useEffect(() => {
    activeEntityIdRef.current = entityId;
    setActiveEntityId(entityId);
  }, [entityId]);

  useEffect(() => {
    threadsRef.current = threads;
  }, [threads]);

  useEffect(() => {
    workspaceRef.current = workspace;
  }, [workspace]);

  const loadThreads = useCallback(
    async (options?: { entityIdOverride?: string }): Promise<ChatThreadSummary[]> => {
      const resolvedEntityId = options?.entityIdOverride ?? activeEntityIdRef.current;
      const response = isGlobalAssistant
        ? await listGlobalChatThreads({ limit: 50 })
        : await listChatThreads(
            resolvedEntityId,
            closeRunId
              ? {
                  closeRunId,
                  limit: 50,
                }
              : {
                  limit: 50,
                },
          );
      setThreads(response.threads);
      setSelectedThread((current) => {
        if (current === null) {
          return current;
        }
        return response.threads.find((thread) => thread.id === current.id) ?? current;
      });
      return response.threads;
    },
    [closeRunId, isGlobalAssistant],
  );

  const loadThreadWorkspace = useCallback(
    async (
      thread: ChatThreadSummary,
      options?: {
        entityIdOverride?: string;
        showLoader?: boolean;
      },
    ): Promise<void> => {
      const requestId = threadLoadRequestIdRef.current + 1;
      threadLoadRequestIdRef.current = requestId;
      const pendingTurnVersionAtStart = pendingTurnVersionRef.current;
      const resolvedEntityId = options?.entityIdOverride ?? thread.entity_id;
      const showLoader = options?.showLoader ?? true;
      setSelectedThread(thread);
      if (showLoader) {
        setIsLoadingThread(true);
      }
      setError(null);

      try {
        const [threadDetail, threadWorkspace] = await Promise.all([
          getChatThread(thread.id, resolvedEntityId),
          getChatThreadWorkspace(thread.id, resolvedEntityId),
        ]);
        if (requestId !== threadLoadRequestIdRef.current) {
          return;
        }
        activeEntityIdRef.current = threadDetail.thread.entity_id;
        setActiveEntityId(threadDetail.thread.entity_id);
        setMessages(threadDetail.messages);
        setWorkspace(threadWorkspace);
        if (pendingTurnVersionRef.current === pendingTurnVersionAtStart) {
          setPendingTurn(null);
        }
        setSelectedThread(threadDetail.thread);
      } catch (caughtError: unknown) {
        if (requestId !== threadLoadRequestIdRef.current) {
          return;
        }
        if (caughtError instanceof ChatApiError && caughtError.status !== 401) {
          setError("The selected chat could not be loaded.");
        }
      } finally {
        if (showLoader && requestId === threadLoadRequestIdRef.current) {
          setIsLoadingThread(false);
        }
      }
    },
    [],
  );

  const createFreshThread = useCallback(
    async (options?: CreateThreadOptions): Promise<ChatThreadSummary | null> => {
      if (isCreatingThreadRef.current) {
        return null;
      }

      isCreatingThreadRef.current = true;
      setIsCreatingThread(true);
      setError(null);
      setPendingTurn(null);

      try {
        const resolvedEntityId = activeEntityIdRef.current;
        const nextThreadTitle = buildNewThreadTitle({
          closeRunId,
          selectedThread: selectedThreadRef.current,
          threads: threadsRef.current,
          workspace: workspaceRef.current,
        });
        const response = isGlobalAssistant
          ? await createGlobalChatThread()
          : await createChatThread(
              closeRunId
                ? {
                    close_run_id: closeRunId,
                    entity_id: resolvedEntityId,
                    title: nextThreadTitle ?? "Close run chat",
                  }
                : {
                    entity_id: resolvedEntityId,
                  },
            );
        const nextThread = response.thread;
        activeEntityIdRef.current = nextThread.entity_id;
        setActiveEntityId(nextThread.entity_id);
        setThreads((current) => dedupeThreads([nextThread, ...current]));
        setMessages([]);
        setWorkspace(null);
        setSelectedThread(nextThread);
        await loadThreadWorkspace(nextThread, {
          entityIdOverride: nextThread.entity_id,
          showLoader: false,
        });
        return nextThread;
      } catch (caughtError: unknown) {
        if (
          !options?.suppressError &&
          caughtError instanceof ChatApiError &&
          caughtError.status !== 401
        ) {
          setError("The assistant could not start a new chat.");
        }
        return null;
      } finally {
        isCreatingThreadRef.current = false;
        setIsCreatingThread(false);
      }
    },
    [closeRunId, isGlobalAssistant, loadThreadWorkspace],
  );

  useEffect(() => {
    let isMounted = true;

    async function bootstrap(): Promise<void> {
      setIsBootstrapping(true);
      setError(null);
      setPendingTurn(null);
      setMessages([]);
      setWorkspace(null);
      setSelectedThread(null);

      try {
        const loadedThreads = await loadThreads();
        if (!isMounted) {
          return;
        }

        if (loadedThreads[0] !== undefined) {
          await loadThreadWorkspace(loadedThreads[0], {
            entityIdOverride: loadedThreads[0].entity_id,
            showLoader: false,
          });
          return;
        }

        if (presentation === "workspace") {
          await createFreshThread({ suppressError: true });
        }
      } catch (caughtError: unknown) {
        if (caughtError instanceof ChatApiError && caughtError.status !== 401) {
          setError("The assistant workspace could not be prepared.");
        }
      } finally {
        if (isMounted) {
          setIsBootstrapping(false);
        }
      }
    }

    void bootstrap();

    return () => {
      isMounted = false;
    };
  }, [createFreshThread, entityId, loadThreadWorkspace, loadThreads, presentation]);

  const renderableMessages = useMemo(() => buildRenderableMessages(messages), [messages]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({
      behavior: renderableMessages.length > 0 || pendingTurn !== null ? "smooth" : "auto",
      block: "end",
    });
  }, [pendingTurn, renderableMessages.length]);

  const refreshSelectedThread = useCallback(
    async (options?: { entityIdOverride?: string }): Promise<void> => {
      const thread = selectedThreadRef.current;
      if (thread === null) {
        return;
      }
      const resolvedEntityId = options?.entityIdOverride ?? thread.entity_id;

      await loadThreadWorkspace(thread, {
        entityIdOverride: resolvedEntityId,
        showLoader: false,
      });
      try {
        await loadThreads({ entityIdOverride: resolvedEntityId });
      } catch {
        // Keep the current thread visible even if the thread index refresh fails.
      }
    },
    [loadThreadWorkspace, loadThreads],
  );

  const confirmThreadDeletion = useCallback(async (): Promise<void> => {
    if (pendingDeletionThread === null) {
      return;
    }

    const thread = pendingDeletionThread;
    setDeletingThreadId(thread.id);
    setPendingDeletionThread(null);

    try {
      await deleteChatThread(thread.id, thread.entity_id);
      const remainingThreads = threads.filter((candidate) => candidate.id !== thread.id);
      setThreads(remainingThreads);
      setError(null);

      if (selectedThread?.id !== thread.id) {
        return;
      }

      if (remainingThreads[0] !== undefined) {
        await loadThreadWorkspace(remainingThreads[0], {
          entityIdOverride: remainingThreads[0].entity_id,
          showLoader: false,
        });
        return;
      }

      setMessages([]);
      setWorkspace(null);
      setSelectedThread(null);
      setPendingTurn(null);

      if (presentation === "workspace") {
        await createFreshThread({ suppressError: true });
      }
    } catch (caughtError: unknown) {
      if (caughtError instanceof ChatApiError && caughtError.status !== 401) {
        setError("The selected chat could not be deleted.");
      }
    } finally {
      setDeletingThreadId(null);
    }
  }, [
    createFreshThread,
    loadThreadWorkspace,
    pendingDeletionThread,
    presentation,
    selectedThread?.id,
    threads,
  ]);

  const isAwaitingReply = pendingTurn !== null && pendingTurn.assistantContent === null;
  const isBusy = isBootstrapping || isLoadingThread;

  return (
    <>
      <div
        style={
          presentation === "workspace"
            ? workbenchShellStyle
            : {
                ...workbenchShellStyle,
                gridTemplateColumns: "minmax(240px, 280px) minmax(0, 1fr)",
              }
        }
      >
        <ThreadSidebar
          deletingThreadId={deletingThreadId}
          isBootstrapping={isBootstrapping}
          isCreatingThread={isCreatingThread}
          selectedThreadId={selectedThread?.id ?? null}
          threads={threads}
          onCreateThread={() => {
            void createFreshThread();
          }}
          onRequestDeleteThread={setPendingDeletionThread}
          onSelectThread={(thread) => {
            void loadThreadWorkspace(thread, {
              entityIdOverride: thread.entity_id,
            });
          }}
        />

        <section style={conversationPaneStyle}>
          <ConversationHeader
            assistantMode={resolvedAssistantMode}
            error={error}
            isCreatingThread={isCreatingThread}
            isLoading={isBusy}
            thread={selectedThread}
            workspace={workspace}
          />

          <MessageList
            assistantMode={resolvedAssistantMode}
            isAwaitingReply={isAwaitingReply}
            isLoading={isBusy}
            messages={renderableMessages}
            pendingTurn={pendingTurn}
          />

          <ActionComposer
            assistantMode={resolvedAssistantMode}
            closeRunId={selectedThread?.close_run_id ?? closeRunId}
            disabled={isBusy || selectedThread === null}
            entityId={activeEntityId}
            onActionStateChange={() => {
              void refreshSelectedThread();
            }}
            onMessageSent={(response: ChatActionResponse, draft: ComposerDraft) => {
              const threadId = selectedThread?.id;
              if (threadId) {
                setMessages((current) =>
                  mergeMessages(
                    current,
                    buildLocalTurnMessages({
                      baseMessageOrder: getHighestMessageOrder(current),
                      draft,
                      response,
                      threadId,
                    }),
                  ),
                );
              }
              setPendingTurn(null);
              activeEntityIdRef.current = response.thread_entity_id;
              setActiveEntityId(response.thread_entity_id);
              void refreshSelectedThread({
                entityIdOverride: response.thread_entity_id,
              });
            }}
            onSubmissionError={(message: string) => {
              setPendingTurn((current) =>
                current === null
                  ? null
                  : {
                      ...current,
                      assistantContent: message,
                    },
              );
            }}
            onSubmissionStart={(draft: ComposerDraft) => {
              pendingTurnVersionRef.current += 1;
              setPendingTurn({
                assistantContent: null,
                draft,
              });
            }}
            threadId={selectedThread?.id ?? ""}
            workspace={workspace}
          />

          <div ref={messagesEndRef} />
        </section>
      </div>

      {pendingDeletionThread !== null ? (
        <DeleteThreadModal
          isDeleting={deletingThreadId === pendingDeletionThread.id}
          thread={pendingDeletionThread}
          onClose={() => {
            if (deletingThreadId === null) {
              setPendingDeletionThread(null);
            }
          }}
          onConfirm={() => {
            void confirmThreadDeletion();
          }}
        />
      ) : null}
    </>
  );
}

type ThreadSidebarProps = {
  deletingThreadId: string | null;
  isBootstrapping: boolean;
  isCreatingThread: boolean;
  onCreateThread: () => void;
  onRequestDeleteThread: (thread: ChatThreadSummary) => void;
  onSelectThread: (thread: ChatThreadSummary) => void;
  selectedThreadId: string | null;
  threads: readonly ChatThreadSummary[];
};

function ThreadSidebar({
  deletingThreadId,
  isBootstrapping,
  isCreatingThread,
  onCreateThread,
  onRequestDeleteThread,
  onSelectThread,
  selectedThreadId,
  threads,
}: Readonly<ThreadSidebarProps>): ReactElement {
  return (
    <aside style={threadSidebarStyle}>
      <div style={threadSidebarHeaderStyle}>
        <div style={sidebarHeadingBlockStyle}>
          <p style={sidebarEyebrowStyle}>Assistant</p>
          <h2 style={sidebarTitleStyle}>Threads</h2>
        </div>

        <button
          disabled={isCreatingThread}
          onClick={onCreateThread}
          style={newChatButtonStyle(isCreatingThread)}
          type="button"
        >
          <QuartzIcon name="sparkle" style={buttonIconStyle} />
          <span>{isCreatingThread ? "Creating..." : "New chat"}</span>
        </button>
      </div>

      {isBootstrapping ? (
        <div style={emptySidebarCardStyle}>
          <p style={emptySidebarTitleStyle}>Preparing assistant</p>
          <p style={emptySidebarBodyStyle}>Loading your latest conversation threads.</p>
        </div>
      ) : threads.length === 0 ? (
        <div style={emptySidebarCardStyle}>
          <p style={emptySidebarTitleStyle}>No chats yet</p>
          <p style={emptySidebarBodyStyle}>Create a chat to start working with the assistant.</p>
        </div>
      ) : (
        <ul style={threadListStyle}>
          {threads.map((thread) => {
            const isActive = thread.id === selectedThreadId;
            const isDeleting = deletingThreadId === thread.id;
            return (
              <li key={thread.id}>
                <div style={threadRowStyle}>
                  <button
                    onClick={() => onSelectThread(thread)}
                    style={threadCardStyle(isActive)}
                    type="button"
                  >
                    <span style={threadTitleStyle}>{formatThreadTitle(thread)}</span>
                    <span style={threadMetaStyle}>
                      {formatThreadSubtitle(thread)}
                      {" / "}
                      {formatThreadTime(thread.updated_at)}
                    </span>
                  </button>

                  <button
                    aria-label={`Delete ${formatThreadTitle(thread)}`}
                    disabled={isDeleting}
                    onClick={() => onRequestDeleteThread(thread)}
                    style={threadDeleteButtonStyle(isDeleting)}
                    title="Delete chat"
                    type="button"
                  >
                    <QuartzIcon name="trash" style={threadDeleteIconStyle} />
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}

type ConversationHeaderProps = {
  assistantMode: "close_run" | "entity" | "global";
  error: string | null;
  isCreatingThread: boolean;
  isLoading: boolean;
  thread: ChatThreadSummary | null;
  workspace: ChatThreadWorkspace | null;
};

function ConversationHeader({
  assistantMode,
  error,
  isCreatingThread,
  isLoading,
  thread,
  workspace,
}: Readonly<ConversationHeaderProps>): ReactElement {
  const assistantLabel =
    assistantMode === "global"
      ? "Global Assistant"
      : assistantMode === "close_run"
        ? "Close Assistant"
        : "Entity Assistant";
  const scopeLabel =
    assistantMode === "global"
      ? "All workspaces"
      : assistantMode === "close_run"
        ? (thread?.grounding.period_label ?? "Close scope")
        : (thread?.grounding.entity_name ?? "Entity scope");
  const activeWorkspaceLabel =
    assistantMode === "global" ? (workspace?.grounding.entity_name ?? null) : null;
  const activePeriodLabel =
    assistantMode === "global" ? (workspace?.grounding.period_label ?? null) : null;
  const recoveryActions = workspace?.memory.recovery_actions ?? [];
  const recoveryState = workspace?.memory.recovery_state ?? null;
  const recoverySummary = workspace?.memory.recovery_summary ?? null;

  return (
    <header style={conversationHeaderStyle}>
      <div style={conversationHeaderTopRowStyle}>
        <div style={conversationTitleBlockStyle}>
          <div style={conversationHeaderMetaStyle}>
            <p style={conversationEyebrowStyle}>{assistantLabel}</p>
            <span style={conversationScopePillStyle}>{scopeLabel}</span>
          </div>
          <h2 style={conversationTitleStyle}>
            {thread === null ? "New chat" : formatThreadTitle(thread)}
          </h2>
          {activeWorkspaceLabel ? (
            <p style={conversationAnchorStyle}>
              Current workspace: {activeWorkspaceLabel}
              {activePeriodLabel ? ` / ${activePeriodLabel}` : ""}
            </p>
          ) : null}
        </div>

        <div style={conversationStatusRowStyle}>
          {isCreatingThread ? <span style={conversationStatusPillStyle}>Starting chat</span> : null}
          {isLoading ? <span style={conversationMutedPillStyle}>Syncing</span> : null}
          {error ? (
            <div role="status" style={conversationErrorStyle}>
              {error}
            </div>
          ) : null}
        </div>
      </div>

      {recoverySummary ? (
        <div style={conversationRecoveryCardStyle(recoveryState)}>
          <div style={conversationRecoveryHeaderStyle}>
            <span style={conversationRecoveryStateStyle(recoveryState)}>
              {formatRecoveryState(recoveryState)}
            </span>
          </div>
          <p style={conversationRecoverySummaryStyle}>{recoverySummary}</p>
          {recoveryActions.length > 0 ? (
            <p style={conversationRecoveryActionsStyle}>{recoveryActions.slice(0, 2).join(" ")}</p>
          ) : null}
        </div>
      ) : null}
    </header>
  );
}

type MessageListProps = {
  assistantMode: "close_run" | "entity" | "global";
  isAwaitingReply: boolean;
  isLoading: boolean;
  messages: readonly RenderableMessage[];
  pendingTurn: PendingTurn | null;
};

function MessageList({
  assistantMode,
  isAwaitingReply,
  isLoading,
  messages,
  pendingTurn,
}: Readonly<MessageListProps>): ReactElement {
  const hasMessages = messages.length > 0 || pendingTurn !== null;

  return (
    <div style={messageListStyle}>
      <div style={messageStreamStyle}>
        {!hasMessages && !isLoading ? (
          <div style={emptyConversationCardStyle}>
            <p style={emptyConversationEyebrowStyle}>Ready</p>
            <h3 style={emptyConversationTitleStyle}>Start a new conversation</h3>
            <p style={emptyConversationBodyStyle}>
              {assistantMode === "global"
                ? "Ask across workspaces, identify what needs attention, or tell me to switch this chat to another workspace."
                : assistantMode === "entity"
                  ? "Ask about this workspace, review its close runs, or decide what to do next."
                  : "Ask for the next close action, resolve a blocker, or upload source documents directly into the thread."}
            </p>
          </div>
        ) : null}

        {messages.map((message) => (
          <article
            key={message.id}
            style={
              message.role === "user" ? userMessageContainerStyle : assistantMessageContainerStyle
            }
          >
            <div
              style={message.role === "user" ? userMessageBubbleStyle : assistantMessageBubbleStyle}
            >
              <div style={messageHeaderStyle}>
                <span style={messageRoleStyle(message.role)}>
                  {message.role === "user" ? "You" : "Assistant"}
                </span>
                <span style={messageTimeStyle}>{message.displayTime}</span>
              </div>

              {extractInlineAttachments(message).length > 0 ? (
                <div style={inlineAttachmentRowStyle}>
                  {extractInlineAttachments(message).map((attachment) => (
                    <span
                      key={`${message.id}-${attachment.filename}`}
                      style={inlineAttachmentPillStyle}
                    >
                      {attachment.intentLabel}: {attachment.filename}
                    </span>
                  ))}
                </div>
              ) : null}

              <p style={messageContentStyle}>{message.content}</p>
            </div>
          </article>
        ))}

        {pendingTurn !== null ? (
          <>
            <article style={userMessageContainerStyle}>
              <div style={userMessageBubbleStyle}>
                <div style={messageHeaderStyle}>
                  <span style={messageRoleStyle("user")}>You</span>
                  <span style={messageTimeStyle}>Sending...</span>
                </div>

                {pendingTurn.draft.attachmentNames.length > 0 ? (
                  <div style={inlineAttachmentRowStyle}>
                    {pendingTurn.draft.attachmentNames.map((attachmentName) => (
                      <span key={attachmentName} style={inlineAttachmentPillStyle}>
                        Source document: {attachmentName}
                      </span>
                    ))}
                  </div>
                ) : null}

                <p style={messageContentStyle}>{pendingTurn.draft.content}</p>
              </div>
            </article>

            <article style={assistantMessageContainerStyle}>
              <div style={assistantMessageBubbleStyle}>
                <div style={messageHeaderStyle}>
                  <span style={messageRoleStyle("assistant")}>Assistant</span>
                  <span style={messageTimeStyle}>
                    {isAwaitingReply ? "Thinking..." : "Reply ready"}
                  </span>
                </div>

                {pendingTurn.assistantContent === null ? (
                  <div style={thinkingBubbleStyle}>
                    <span className="quartz-chat-thinking-dots" aria-hidden="true">
                      <span />
                      <span />
                      <span />
                    </span>
                    <span>Working through the request</span>
                  </div>
                ) : (
                  <p style={messageContentStyle}>{pendingTurn.assistantContent}</p>
                )}
              </div>
            </article>
          </>
        ) : null}
      </div>
    </div>
  );
}

type DeleteThreadModalProps = {
  isDeleting: boolean;
  onClose: () => void;
  onConfirm: () => void;
  thread: ChatThreadSummary;
};

function DeleteThreadModal({
  isDeleting,
  onClose,
  onConfirm,
  thread,
}: Readonly<DeleteThreadModalProps>): ReactElement {
  return (
    <div aria-modal="true" className="quartz-modal-backdrop" onClick={onClose} role="dialog">
      <div
        className="quartz-modal-card"
        onClick={(event) => event.stopPropagation()}
        role="document"
      >
        <div style={deleteModalHeaderStyle}>
          <div style={{ display: "grid", gap: 8 }}>
            <h2 style={deleteModalTitleStyle}>Delete this chat?</h2>
            <p style={deleteModalBodyStyle}>
              Remove <strong>{formatThreadTitle(thread)}</strong> and its full conversation history.
            </p>
          </div>
          <button aria-label="Close" className="quartz-icon-button" onClick={onClose} type="button">
            <QuartzIcon name="dismiss" />
          </button>
        </div>

        <div className="quartz-form-row quartz-modal-actions">
          <button className="secondary-button" onClick={onClose} type="button">
            Cancel
          </button>
          <button
            className="primary-button"
            disabled={isDeleting}
            onClick={onConfirm}
            style={deleteModalDangerButtonStyle}
            type="button"
          >
            {isDeleting ? "Deleting..." : "Delete chat"}
          </button>
        </div>
      </div>
    </div>
  );
}

function buildRenderableMessages(messages: readonly ChatMessageRecord[]): RenderableMessage[] {
  return messages
    .map((message, index) => ({ index, message }))
    .filter(
      ({ message }) => message.role !== "system" && !looksLikeInternalContextDump(message.content),
    )
    .sort((left, right) => {
      const leftOrder = left.message.message_order;
      const rightOrder = right.message.message_order;
      if (Number.isFinite(leftOrder) && Number.isFinite(rightOrder) && leftOrder !== rightOrder) {
        return leftOrder - rightOrder;
      }
      const leftTime = Date.parse(left.message.created_at);
      const rightTime = Date.parse(right.message.created_at);
      if (!Number.isNaN(leftTime) && !Number.isNaN(rightTime) && leftTime !== rightTime) {
        return leftTime - rightTime;
      }
      return left.index - right.index;
    })
    .map(({ message }) => ({
      ...message,
      displayTime: formatMessageTime(message.created_at),
    }));
}

function buildLocalTurnMessages(options: {
  baseMessageOrder: number;
  draft: ComposerDraft;
  response: ChatActionResponse;
  threadId: string;
}): readonly ChatMessageRecord[] {
  const userCreatedAt = new Date();
  const assistantCreatedAt = new Date(userCreatedAt.getTime() + 1);
  const attachmentPayload =
    options.draft.attachmentNames.length > 0
      ? {
          attachments: options.draft.attachmentNames.map((filename) => ({
            filename,
            intent: "source_documents",
          })),
        }
      : {};

  return [
    {
      content: options.draft.content,
      created_at: userCreatedAt.toISOString(),
      grounding_payload: attachmentPayload,
      id: `optimistic-user:${options.response.message_id}`,
      linked_action_id: null,
      message_type: "action",
      message_order: options.baseMessageOrder + 1,
      model_metadata: null,
      role: "user",
      thread_id: options.threadId,
    },
    {
      content: options.response.content,
      created_at: assistantCreatedAt.toISOString(),
      grounding_payload: {},
      id: options.response.message_id,
      linked_action_id: options.response.action_plan?.id ?? null,
      message_type: options.response.is_read_only ? "analysis" : "action",
      message_order: options.baseMessageOrder + 2,
      model_metadata: null,
      role: "assistant",
      thread_id: options.threadId,
    },
  ];
}

function mergeMessages(
  existing: readonly ChatMessageRecord[],
  incoming: readonly ChatMessageRecord[],
): ChatMessageRecord[] {
  const merged = [...existing];
  const seenIds = new Set(existing.map((message) => message.id));
  for (const message of incoming) {
    if (seenIds.has(message.id)) {
      continue;
    }
    seenIds.add(message.id);
    merged.push(message);
  }
  return merged;
}

function getHighestMessageOrder(messages: readonly ChatMessageRecord[]): number {
  let highestOrder = 0;
  for (const message of messages) {
    if (Number.isFinite(message.message_order) && message.message_order > highestOrder) {
      highestOrder = message.message_order;
    }
  }
  return highestOrder;
}

function extractInlineAttachments(message: ChatMessageRecord): Array<{
  filename: string;
  intentLabel: string;
}> {
  const rawAttachments = message.grounding_payload.attachments;
  if (!Array.isArray(rawAttachments)) {
    return [];
  }

  return rawAttachments
    .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    .map((item) => ({
      filename: typeof item.filename === "string" ? item.filename : "attached file",
      intentLabel:
        typeof item.intent === "string" ? item.intent.replaceAll("_", " ") : "attachment",
    }));
}

function looksLikeInternalContextDump(content: string): boolean {
  const markers = [
    "Close run status=",
    "OperatingMode=",
    "Documents=",
    "Recommendations=",
    "Journals=",
    "Reconciliations=",
    "SupportingSchedules=",
    "Pending chat approvals=",
    "Evidence pack not yet assembled",
  ];
  return markers.filter((marker) => content.includes(marker)).length >= 3;
}

function dedupeThreads(threads: readonly ChatThreadSummary[]): ChatThreadSummary[] {
  const seen = new Set<string>();
  const result: ChatThreadSummary[] = [];

  for (const thread of threads) {
    if (seen.has(thread.id)) {
      continue;
    }
    seen.add(thread.id);
    result.push(thread);
  }

  return result;
}

function formatThreadTitle(thread: ChatThreadSummary): string {
  const title = thread.title?.trim();
  if (!title || title === "Accounting agent workspace") {
    return "New chat";
  }
  return title;
}

function formatThreadSubtitle(thread: ChatThreadSummary): string {
  const scope = thread.grounding.period_label ?? "Entity scope";
  if (thread.message_count <= 1) {
    return scope;
  }
  const messageCountLabel =
    thread.message_count === 1 ? "1 message" : `${thread.message_count} messages`;
  return `${scope} / ${messageCountLabel}`;
}

function formatThreadTime(value: string): string {
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) {
    return "Updated recently";
  }

  const differenceInMinutes = Math.max(0, Math.round((Date.now() - parsed) / 60000));
  if (differenceInMinutes < 1) {
    return "Just now";
  }
  if (differenceInMinutes < 60) {
    return `${differenceInMinutes}m ago`;
  }
  const differenceInHours = Math.round(differenceInMinutes / 60);
  if (differenceInHours < 24) {
    return `${differenceInHours}h ago`;
  }
  const differenceInDays = Math.round(differenceInHours / 24);
  return `${differenceInDays}d ago`;
}

function formatMessageTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) {
    return value;
  }

  return new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed);
}

function formatRecoveryState(value: string | null): string {
  if (value === "attention_required") {
    return "Attention required";
  }
  if (value === "resuming") {
    return "Resuming";
  }
  if (value === "working") {
    return "Working";
  }
  if (value === "paused") {
    return "Paused";
  }
  return "Recovery";
}

function buildNewThreadTitle(options: {
  closeRunId: string | undefined;
  selectedThread: ChatThreadSummary | null;
  threads: readonly ChatThreadSummary[];
  workspace: ChatThreadWorkspace | null;
}): string | undefined {
  if (!options.closeRunId) {
    return undefined;
  }

  const periodLabel =
    options.selectedThread?.grounding.period_label ??
    options.workspace?.grounding.period_label ??
    "Close run";
  const threadNumber = options.threads.length + 1;
  return `${periodLabel} chat ${threadNumber}`;
}

const workbenchShellStyle = {
  display: "grid",
  gridTemplateColumns: "minmax(272px, 312px) minmax(0, 1fr)",
  height: "100%",
  minHeight: 0,
  overflow: "hidden",
} satisfies CSSProperties;

const threadSidebarStyle = {
  background:
    "linear-gradient(180deg, rgba(247, 243, 242, 0.94) 0%, rgba(241, 237, 236, 0.98) 100%)",
  borderRight: "1px solid var(--quartz-border)",
  display: "grid",
  gap: 18,
  minHeight: 0,
  padding: 20,
} satisfies CSSProperties;

const threadSidebarHeaderStyle = {
  display: "grid",
  gap: 14,
} satisfies CSSProperties;

const sidebarHeadingBlockStyle = {
  display: "grid",
  gap: 4,
} satisfies CSSProperties;

const sidebarEyebrowStyle = {
  color: "var(--quartz-secondary)",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
} satisfies CSSProperties;

const sidebarTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 26,
  fontWeight: 600,
  letterSpacing: "-0.05em",
  lineHeight: 1.05,
  margin: 0,
} satisfies CSSProperties;

function newChatButtonStyle(disabled: boolean) {
  return {
    alignItems: "center",
    border: "1px solid var(--quartz-primary)",
    borderRadius: 999,
    background: "var(--quartz-primary)",
    color: "var(--quartz-primary-contrast)",
    cursor: disabled ? "not-allowed" : "pointer",
    display: "inline-flex",
    gap: 8,
    justifyContent: "center",
    minHeight: 40,
    opacity: disabled ? 0.72 : 1,
    padding: "0 16px",
    width: "100%",
  } satisfies CSSProperties;
}

const buttonIconStyle = {
  height: 15,
  width: 15,
} satisfies CSSProperties;

const emptySidebarCardStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 18,
  background: "rgba(255, 255, 255, 0.88)",
  display: "grid",
  gap: 8,
  padding: 18,
} satisfies CSSProperties;

const emptySidebarTitleStyle = {
  color: "var(--quartz-ink)",
  fontSize: 15,
  fontWeight: 700,
  margin: 0,
} satisfies CSSProperties;

const emptySidebarBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
} satisfies CSSProperties;

const threadListStyle = {
  display: "grid",
  gap: 10,
  listStyle: "none",
  margin: 0,
  minHeight: 0,
  overflow: "auto",
  padding: 0,
} satisfies CSSProperties;

const threadRowStyle = {
  alignItems: "stretch",
  display: "grid",
  gap: 8,
  gridTemplateColumns: "minmax(0, 1fr) auto",
} satisfies CSSProperties;

function threadCardStyle(active: boolean) {
  return {
    border: active ? "1px solid rgba(69, 97, 123, 0.28)" : "1px solid var(--quartz-border)",
    borderRadius: 16,
    background: active ? "rgba(69, 97, 123, 0.09)" : "rgba(255, 255, 255, 0.8)",
    boxShadow: active ? "inset 3px 0 0 var(--quartz-secondary)" : "none",
    color: "var(--quartz-ink)",
    cursor: "pointer",
    display: "grid",
    gap: 6,
    minHeight: 74,
    padding: "14px 16px",
    textAlign: "left",
    width: "100%",
  } satisfies CSSProperties;
}

const threadTitleStyle = {
  color: "var(--quartz-ink)",
  fontSize: 14,
  fontWeight: 600,
  lineHeight: "20px",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} satisfies CSSProperties;

const threadMetaStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
  lineHeight: "18px",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} satisfies CSSProperties;

function threadDeleteButtonStyle(disabled: boolean) {
  return {
    alignItems: "center",
    border: "1px solid rgba(123, 45, 38, 0.18)",
    borderRadius: 14,
    background: "rgba(255, 255, 255, 0.82)",
    color: "var(--quartz-error)",
    cursor: disabled ? "not-allowed" : "pointer",
    display: "inline-flex",
    justifyContent: "center",
    minWidth: 42,
    opacity: disabled ? 0.6 : 1,
  } satisfies CSSProperties;
}

const threadDeleteIconStyle = {
  height: 16,
  width: 16,
} satisfies CSSProperties;

const conversationPaneStyle = {
  background: "linear-gradient(180deg, rgba(253, 248, 248, 0.98) 0%, rgba(252, 252, 250, 1) 100%)",
  display: "grid",
  gridTemplateRows: "auto minmax(0, 1fr) auto",
  minHeight: 0,
} satisfies CSSProperties;

const conversationHeaderStyle = {
  borderBottom: "1px solid var(--quartz-border)",
  display: "grid",
  gap: 14,
  padding: "22px 28px 16px",
} satisfies CSSProperties;

const conversationHeaderTopRowStyle = {
  alignItems: "flex-start",
  display: "flex",
  gap: 16,
  justifyContent: "space-between",
} satisfies CSSProperties;

const conversationTitleBlockStyle = {
  display: "grid",
  gap: 8,
  minWidth: 0,
} satisfies CSSProperties;

const conversationHeaderMetaStyle = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 10,
} satisfies CSSProperties;

const conversationEyebrowStyle = {
  color: "var(--quartz-secondary)",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
} satisfies CSSProperties;

const conversationScopePillStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  color: "var(--quartz-muted)",
  fontSize: 12,
  fontWeight: 600,
  padding: "5px 10px",
} satisfies CSSProperties;

const conversationTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 34,
  fontWeight: 600,
  letterSpacing: "-0.06em",
  lineHeight: 1.02,
  margin: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} satisfies CSSProperties;

const conversationAnchorStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
  lineHeight: "18px",
  margin: 0,
} satisfies CSSProperties;

const conversationStatusRowStyle = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  justifyContent: "flex-end",
} satisfies CSSProperties;

function conversationRecoveryCardStyle(recoveryState: string | null) {
  const accentColor =
    recoveryState === "attention_required"
      ? "rgba(123, 45, 38, 0.24)"
      : recoveryState === "paused"
        ? "rgba(142, 115, 75, 0.24)"
        : "rgba(69, 97, 123, 0.18)";
  const backgroundColor =
    recoveryState === "attention_required"
      ? "rgba(255, 245, 243, 0.94)"
      : recoveryState === "paused"
        ? "rgba(255, 250, 244, 0.94)"
        : "rgba(244, 248, 252, 0.92)";

  return {
    background: backgroundColor,
    border: `1px solid ${accentColor}`,
    borderRadius: 16,
    display: "grid",
    gap: 6,
    marginTop: 14,
    padding: "12px 14px",
  } satisfies CSSProperties;
}

const conversationRecoveryHeaderStyle = {
  alignItems: "center",
  display: "flex",
  gap: 10,
  justifyContent: "space-between",
} satisfies CSSProperties;

function conversationRecoveryStateStyle(recoveryState: string | null) {
  const color =
    recoveryState === "attention_required"
      ? "var(--quartz-danger)"
      : recoveryState === "paused"
        ? "var(--quartz-accent)"
        : "var(--quartz-secondary)";

  return {
    color,
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: "0.08em",
    textTransform: "uppercase",
  } satisfies CSSProperties;
}

const conversationRecoverySummaryStyle = {
  color: "var(--quartz-ink)",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
} satisfies CSSProperties;

const conversationRecoveryActionsStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
  lineHeight: "18px",
  margin: 0,
} satisfies CSSProperties;

const conversationStatusPillStyle = {
  border: "1px solid rgba(69, 97, 123, 0.22)",
  borderRadius: 999,
  background: "rgba(69, 97, 123, 0.08)",
  color: "var(--quartz-secondary)",
  fontSize: 12,
  fontWeight: 600,
  padding: "6px 10px",
} satisfies CSSProperties;

const conversationMutedPillStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  color: "var(--quartz-muted)",
  fontSize: 12,
  fontWeight: 600,
  padding: "6px 10px",
} satisfies CSSProperties;

const conversationErrorStyle = {
  border: "1px solid rgba(123, 45, 38, 0.22)",
  borderRadius: 999,
  background: "rgba(255, 218, 214, 0.72)",
  color: "var(--quartz-error)",
  fontSize: 12,
  fontWeight: 600,
  lineHeight: "18px",
  padding: "6px 10px",
} satisfies CSSProperties;

const messageListStyle = {
  minHeight: 0,
  overflow: "auto",
  padding: "28px 32px 24px",
} satisfies CSSProperties;

const messageStreamStyle = {
  display: "grid",
  gap: 18,
  margin: "0 auto",
  maxWidth: 960,
  width: "100%",
} satisfies CSSProperties;

const emptyConversationCardStyle = {
  alignSelf: "center",
  border: "1px solid var(--quartz-border)",
  borderRadius: 24,
  background: "rgba(255, 255, 255, 0.84)",
  display: "grid",
  gap: 10,
  marginTop: 36,
  padding: "28px 30px",
  textAlign: "center",
} satisfies CSSProperties;

const emptyConversationEyebrowStyle = {
  color: "var(--quartz-secondary)",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
} satisfies CSSProperties;

const emptyConversationTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 30,
  fontWeight: 600,
  letterSpacing: "-0.05em",
  margin: 0,
} satisfies CSSProperties;

const emptyConversationBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 14,
  lineHeight: "22px",
  margin: 0,
  maxWidth: 520,
} satisfies CSSProperties;

const assistantMessageContainerStyle = {
  display: "flex",
  justifyContent: "flex-start",
} satisfies CSSProperties;

const userMessageContainerStyle = {
  display: "flex",
  justifyContent: "flex-end",
} satisfies CSSProperties;

const assistantMessageBubbleStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: "24px 24px 24px 10px",
  background: "rgba(255, 255, 255, 0.92)",
  display: "grid",
  gap: 10,
  maxWidth: "min(100%, 840px)",
  padding: "18px 20px",
  width: "min(100%, 840px)",
} satisfies CSSProperties;

const userMessageBubbleStyle = {
  border: "1px solid rgba(69, 97, 123, 0.2)",
  borderRadius: "24px 24px 10px 24px",
  background: "rgba(69, 97, 123, 0.08)",
  display: "grid",
  gap: 10,
  maxWidth: "min(100%, 680px)",
  padding: "18px 20px",
} satisfies CSSProperties;

const messageHeaderStyle = {
  alignItems: "center",
  display: "flex",
  gap: 10,
  justifyContent: "space-between",
} satisfies CSSProperties;

function messageRoleStyle(role: ChatMessageRecord["role"]) {
  return {
    color: role === "user" ? "var(--quartz-secondary)" : "var(--quartz-ink)",
    fontSize: 12,
    fontWeight: 700,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
  } satisfies CSSProperties;
}

const messageTimeStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
} satisfies CSSProperties;

const messageContentStyle = {
  color: "var(--quartz-ink)",
  fontSize: 15,
  lineHeight: "25px",
  margin: 0,
  overflowWrap: "anywhere",
  whiteSpace: "pre-wrap",
} satisfies CSSProperties;

const inlineAttachmentRowStyle = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
} satisfies CSSProperties;

const inlineAttachmentPillStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  background: "var(--quartz-surface-low)",
  color: "var(--quartz-muted)",
  fontSize: 11,
  fontWeight: 600,
  padding: "6px 10px",
} satisfies CSSProperties;

const thinkingBubbleStyle = {
  alignItems: "center",
  color: "var(--quartz-muted)",
  display: "inline-flex",
  gap: 10,
  fontSize: 14,
  lineHeight: "22px",
} satisfies CSSProperties;

const deleteModalHeaderStyle = {
  alignItems: "flex-start",
  display: "flex",
  gap: 16,
  justifyContent: "space-between",
} satisfies CSSProperties;

const deleteModalTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 26,
  fontWeight: 600,
  letterSpacing: "-0.04em",
  margin: 0,
} satisfies CSSProperties;

const deleteModalBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 14,
  lineHeight: "22px",
  margin: 0,
} satisfies CSSProperties;

const deleteModalDangerButtonStyle = {
  background: "var(--quartz-error)",
  borderColor: "var(--quartz-error)",
} satisfies CSSProperties;
