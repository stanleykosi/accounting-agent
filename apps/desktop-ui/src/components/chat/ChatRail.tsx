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
  readChatThreadListSnapshot,
  readChatThreadSnapshot,
  readChatThreadWorkspaceSnapshot,
  readGlobalChatThreadListSnapshot,
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

type ThreadScopeFilter = "all" | "workspace" | "close";

type InitialChatRailState = {
  hasHydratedState: boolean;
  messages: ChatMessageRecord[];
  selectedThread: ChatThreadSummary | null;
  threads: ChatThreadSummary[];
  workspace: ChatThreadWorkspace | null;
};

const THREAD_SCOPE_FILTERS: ReadonlyArray<{ label: string; value: ThreadScopeFilter }> = [
  { label: "All", value: "all" },
  { label: "Workspace", value: "workspace" },
  { label: "Close", value: "close" },
];

export function ChatRail({
  assistantMode,
  closeRunId,
  entityId,
  presentation = "rail",
}: Readonly<ChatRailProps>): ReactElement {
  const resolvedAssistantMode = assistantMode ?? (closeRunId ? "close_run" : "entity");
  const isGlobalAssistant = resolvedAssistantMode === "global";
  const initialState = useMemo(
    () =>
      readInitialChatRailState({
        entityId,
        isGlobalAssistant,
        ...(closeRunId === undefined ? {} : { closeRunId }),
      }),
    [closeRunId, entityId, isGlobalAssistant],
  );
  const [activeEntityId, setActiveEntityId] = useState(entityId);
  const [threads, setThreads] = useState<ChatThreadSummary[]>(() => initialState.threads);
  const [selectedThread, setSelectedThread] = useState<ChatThreadSummary | null>(
    () => initialState.selectedThread,
  );
  const [messages, setMessages] = useState<ChatMessageRecord[]>(() => initialState.messages);
  const [workspace, setWorkspace] = useState<ChatThreadWorkspace | null>(
    () => initialState.workspace,
  );
  const [pendingTurn, setPendingTurn] = useState<PendingTurn | null>(null);
  const [pendingDeletionThread, setPendingDeletionThread] = useState<ChatThreadSummary | null>(
    null,
  );
  const [deletingThreadId, setDeletingThreadId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBootstrapping, setIsBootstrapping] = useState(!initialState.hasHydratedState);
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
  const hasHydratedStateRef = useRef(initialState.hasHydratedState);

  useEffect(() => {
    hasHydratedStateRef.current = initialState.hasHydratedState;
    if (!initialState.hasHydratedState) {
      return;
    }

    setThreads(initialState.threads);
    setSelectedThread(initialState.selectedThread);
    setMessages(initialState.messages);
    setWorkspace(initialState.workspace);
    setIsBootstrapping(false);
  }, [initialState]);

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
      const keepHydratedState = hasHydratedStateRef.current;
      setIsBootstrapping(!keepHydratedState);
      setError(null);
      setPendingTurn(null);
      if (!keepHydratedState) {
        setMessages([]);
        setWorkspace(null);
        setSelectedThread(null);
      }

      try {
        const loadedThreads = await loadThreads();
        if (!isMounted) {
          return;
        }

        if (loadedThreads[0] !== undefined) {
          await loadThreadWorkspace(loadedThreads[0], {
            entityIdOverride: loadedThreads[0].entity_id,
            showLoader: !keepHydratedState,
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
              gridTemplateColumns: "minmax(170px, 200px) minmax(0, 1fr)",
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
  const [searchQuery, setSearchQuery] = useState("");
  const [scopeFilter, setScopeFilter] = useState<ThreadScopeFilter>("all");
  const visibleThreads = useMemo(
    () => filterThreads({ query: searchQuery, scope: scopeFilter, threads }),
    [scopeFilter, searchQuery, threads],
  );

  return (
    <aside style={threadSidebarStyle}>
      <div style={threadSidebarHeaderStyle}>
        <div style={threadToolbarStyle}>
          <label style={threadSearchShellStyle}>
            <span style={visuallyHiddenStyle}>Search threads</span>
            <QuartzIcon name="search" style={threadSearchIconStyle} />
            <input
              aria-label="Search threads"
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Search threads"
              style={threadSearchInputStyle}
              value={searchQuery}
            />
          </label>
          <button
            aria-label={isCreatingThread ? "Creating chat" : "New chat"}
            disabled={isCreatingThread}
            onClick={onCreateThread}
            style={newChatButtonStyle(isCreatingThread)}
            title={isCreatingThread ? "Creating chat" : "New chat"}
            type="button"
          >
            <QuartzIcon name="sparkle" style={buttonIconStyle} />
          </button>
        </div>

        <div aria-label="Thread scope" role="tablist" style={threadScopeTabsStyle}>
          {THREAD_SCOPE_FILTERS.map((filter) => (
            <button
              aria-selected={scopeFilter === filter.value}
              key={filter.value}
              onClick={() => setScopeFilter(filter.value)}
              role="tab"
              style={threadScopeTabStyle(scopeFilter === filter.value)}
              type="button"
            >
              {filter.label}
            </button>
          ))}
        </div>
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
      ) : visibleThreads.length === 0 ? (
        <div style={emptySidebarCardStyle}>
          <p style={emptySidebarTitleStyle}>No matching threads</p>
          <p style={emptySidebarBodyStyle}>Adjust the search to find a chat.</p>
        </div>
      ) : (
        <ul style={threadListStyle}>
          {visibleThreads.map((thread) => {
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
  const scopeLabel =
    assistantMode === "global"
      ? buildWorkspaceLabel(workspace, "All workspaces")
      : assistantMode === "close_run"
        ? buildThreadScopeLabel(thread, "Close scope")
        : buildThreadScopeLabel(thread, "Entity scope");
  const toolsAvailable = workspace?.tools.length ?? workspace?.mcp_manifest.tools.length ?? 0;

  return (
    <header style={conversationHeaderStyle}>
      <div style={conversationHeaderTopRowStyle}>
        <div style={conversationTitleBlockStyle}>
          <h2 style={conversationTitleStyle}>
            {thread === null ? "New chat" : formatThreadTitle(thread)}
          </h2>
        </div>

        <div style={conversationStatusRowStyle}>
          <span style={conversationScopePillStyle}>{scopeLabel}</span>
          <span style={conversationToolbarPillStyle}>
            <QuartzIcon name="settings" style={conversationToolbarIconStyle} />
            {toolsAvailable > 0 ? `${toolsAvailable} tools available` : "Tools available"}
          </span>
          {isCreatingThread ? <span style={conversationStatusPillStyle}>Starting chat</span> : null}
          {isLoading ? <span style={conversationMutedPillStyle}>Syncing</span> : null}
          {error ? (
            <div role="status" style={conversationErrorStyle}>
              {error}
            </div>
          ) : null}
        </div>
      </div>
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
            {message.role === "assistant" ? (
              <div aria-hidden="true" style={assistantAvatarStyle}>
                <QuartzIcon name="assistant" style={assistantAvatarIconStyle} />
              </div>
            ) : null}
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
              <div aria-hidden="true" style={assistantAvatarStyle}>
                <QuartzIcon name="assistant" style={assistantAvatarIconStyle} />
              </div>
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

function readInitialChatRailState(
  options: Readonly<{
    closeRunId?: string;
    entityId: string;
    isGlobalAssistant: boolean;
  }>,
): InitialChatRailState {
  const threadResponse = options.isGlobalAssistant
    ? readGlobalChatThreadListSnapshot({ limit: 50 })
    : readChatThreadListSnapshot(
      options.entityId,
      options.closeRunId
        ? {
          closeRunId: options.closeRunId,
          limit: 50,
        }
        : {
          limit: 50,
        },
    );
  const threads = threadResponse?.threads ?? [];
  const selectedThread = threads[0] ?? null;
  if (selectedThread === null) {
    return {
      hasHydratedState: threadResponse !== null,
      messages: [],
      selectedThread: null,
      threads,
      workspace: null,
    };
  }

  const threadDetail = readChatThreadSnapshot(selectedThread.id, selectedThread.entity_id);
  const workspace = readChatThreadWorkspaceSnapshot(selectedThread.id, selectedThread.entity_id);
  return {
    hasHydratedState: threadResponse !== null || threadDetail !== null || workspace !== null,
    messages: threadDetail?.messages ?? [],
    selectedThread: threadDetail?.thread ?? selectedThread,
    threads,
    workspace,
  };
}

function filterThreads(options: {
  query: string;
  scope: ThreadScopeFilter;
  threads: readonly ChatThreadSummary[];
}): ChatThreadSummary[] {
  const normalizedQuery = options.query.trim().toLowerCase();
  return options.threads.filter((thread) => {
    if (options.scope === "close" && thread.close_run_id === null) {
      return false;
    }
    if (options.scope === "workspace" && thread.close_run_id !== null) {
      return false;
    }
    if (!normalizedQuery) {
      return true;
    }
    const searchable = [
      formatThreadTitle(thread),
      thread.grounding.entity_name,
      thread.grounding.period_label ?? "",
    ]
      .join(" ")
      .toLowerCase();
    return searchable.includes(normalizedQuery);
  });
}

function buildThreadScopeLabel(thread: ChatThreadSummary | null, fallback: string): string {
  if (thread === null) {
    return fallback;
  }
  const entityName = thread.grounding.entity_name.trim();
  const periodLabel = thread.grounding.period_label?.trim();
  if (entityName && periodLabel) {
    return `${entityName} / ${periodLabel}`;
  }
  if (entityName) {
    return entityName;
  }
  return periodLabel || fallback;
}

function buildWorkspaceLabel(workspace: ChatThreadWorkspace | null, fallback: string): string {
  if (workspace === null) {
    return fallback;
  }
  const entityName = workspace.grounding.entity_name.trim();
  const periodLabel = workspace.grounding.period_label?.trim();
  if (entityName && periodLabel) {
    return `${entityName} / ${periodLabel}`;
  }
  if (entityName) {
    return entityName;
  }
  return periodLabel || fallback;
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
  gridTemplateColumns: "minmax(200px, 240px) minmax(0, 1fr)",
  height: "100%",
  minHeight: 0,
  overflow: "hidden",
} satisfies CSSProperties;

const threadSidebarStyle = {
  background:
    "linear-gradient(180deg, rgba(247, 243, 242, 0.94) 0%, rgba(241, 237, 236, 0.98) 100%)",
  borderRight: "1px solid var(--quartz-border)",
  display: "grid",
  gap: 0,
  gridTemplateRows: "auto minmax(0, 1fr)",
  minHeight: 0,
  overflow: "hidden",
  padding: "10px 8px 6px",
} satisfies CSSProperties;

const threadSidebarHeaderStyle = {
  display: "grid",
  gap: 6,
  paddingBottom: 6,
} satisfies CSSProperties;

function newChatButtonStyle(disabled: boolean) {
  return {
    alignItems: "center",
    border: "1px solid var(--quartz-primary)",
    borderRadius: 8,
    background: "var(--quartz-primary)",
    color: "var(--quartz-primary-contrast)",
    cursor: disabled ? "not-allowed" : "pointer",
    display: "inline-flex",
    flex: "0 0 auto",
    justifyContent: "center",
    height: 32,
    opacity: disabled ? 0.72 : 1,
    padding: 0,
    transition: "opacity 150ms ease",
    width: 32,
  } satisfies CSSProperties;
}

const buttonIconStyle = {
  height: 13,
  width: 13,
} satisfies CSSProperties;

const threadToolbarStyle = {
  alignItems: "center",
  display: "flex",
  gap: 6,
} satisfies CSSProperties;

const threadSearchShellStyle = {
  alignItems: "center",
  background: "rgba(255, 255, 255, 0.86)",
  border: "1px solid var(--quartz-border)",
  borderRadius: 8,
  display: "flex",
  flex: "1 1 0%",
  gap: 6,
  height: 32,
  minWidth: 0,
  padding: "0 8px",
} satisfies CSSProperties;

const threadSearchIconStyle = {
  color: "var(--quartz-muted)",
  flex: "0 0 auto",
  height: 13,
  width: 13,
} satisfies CSSProperties;

const threadSearchInputStyle = {
  background: "transparent",
  border: "none",
  color: "var(--quartz-ink)",
  font: "inherit",
  fontSize: 12,
  minWidth: 0,
  outline: "none",
  padding: 0,
  width: "100%",
} satisfies CSSProperties;

const threadScopeTabsStyle = {
  borderBottom: "1px solid var(--quartz-border)",
  display: "grid",
  gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
  marginTop: 2,
} satisfies CSSProperties;

function threadScopeTabStyle(active: boolean) {
  return {
    background: "transparent",
    border: "none",
    borderBottom: active ? "2px solid var(--quartz-secondary)" : "2px solid transparent",
    color: active ? "var(--quartz-secondary)" : "var(--quartz-muted)",
    cursor: "pointer",
    fontSize: 11,
    fontWeight: active ? 700 : 500,
    minHeight: 28,
    padding: 0,
    transition: "color 150ms ease, border-color 150ms ease",
  } satisfies CSSProperties;
}

const emptySidebarCardStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 10,
  background: "rgba(255, 255, 255, 0.88)",
  display: "grid",
  gap: 4,
  margin: "8px 0",
  padding: "12px 10px",
} satisfies CSSProperties;

const emptySidebarTitleStyle = {
  color: "var(--quartz-ink)",
  fontSize: 12,
  fontWeight: 700,
  margin: 0,
} satisfies CSSProperties;

const emptySidebarBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 11,
  lineHeight: "16px",
  margin: 0,
} satisfies CSSProperties;

const threadListStyle = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  justifyContent: "flex-start",
  listStyle: "none",
  margin: 0,
  minHeight: 0,
  overflow: "auto",
  padding: "4px 0 2px",
} satisfies CSSProperties;

const threadRowStyle = {
  alignItems: "stretch",
  display: "block",
  position: "relative",
} satisfies CSSProperties;

function threadCardStyle(active: boolean) {
  return {
    alignItems: "center",
    border: active ? "1px solid rgba(142, 115, 75, 0.35)" : "1px solid transparent",
    borderRadius: 6,
    background: active ? "rgba(255, 251, 235, 0.78)" : "transparent",
    boxShadow: active
      ? "inset 3px 0 0 var(--quartz-gold)"
      : "none",
    color: "var(--quartz-ink)",
    cursor: "pointer",
    display: "grid",
    minHeight: 0,
    padding: "5px 28px 5px 8px",
    textAlign: "left",
    transition: "background 120ms ease, border-color 120ms ease",
    width: "100%",
  } satisfies CSSProperties;
}

const threadTitleStyle = {
  color: "var(--quartz-ink)",
  display: "-webkit-box",
  fontSize: 12,
  fontWeight: 600,
  lineHeight: "16px",
  overflow: "hidden",
  textOverflow: "ellipsis",
  WebkitBoxOrient: "vertical",
  WebkitLineClamp: 2,
  wordBreak: "break-word",
} satisfies CSSProperties;

function threadDeleteButtonStyle(disabled: boolean) {
  return {
    alignItems: "center",
    background: "transparent",
    border: "none",
    borderRadius: 6,
    color: "var(--quartz-muted)",
    cursor: disabled ? "not-allowed" : "pointer",
    display: "inline-flex",
    height: 22,
    justifyContent: "center",
    opacity: disabled ? 0.4 : 0.5,
    position: "absolute",
    right: 6,
    top: "50%",
    transform: "translateY(-50%)",
    transition: "opacity 120ms ease, color 120ms ease",
    width: 22,
  } satisfies CSSProperties;
}

const threadDeleteIconStyle = {
  height: 13,
  width: 13,
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
  gap: 4,
  padding: "8px 20px",
} satisfies CSSProperties;

const conversationHeaderTopRowStyle = {
  alignItems: "center",
  display: "flex",
  gap: 12,
  justifyContent: "space-between",
  minHeight: 36,
} satisfies CSSProperties;

const conversationTitleBlockStyle = {
  display: "grid",
  minWidth: 0,
  flex: "1 1 0%",
} satisfies CSSProperties;

const conversationScopePillStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 6,
  color: "var(--quartz-muted)",
  fontSize: 11,
  fontWeight: 600,
  maxWidth: 260,
  overflow: "hidden",
  padding: "4px 8px",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} satisfies CSSProperties;

const conversationTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 17,
  fontWeight: 600,
  letterSpacing: "-0.02em",
  lineHeight: 1.2,
  margin: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
} satisfies CSSProperties;

const conversationStatusRowStyle = {
  alignItems: "center",
  display: "flex",
  flex: "0 0 auto",
  flexWrap: "wrap",
  gap: 6,
  justifyContent: "flex-end",
} satisfies CSSProperties;

const conversationToolbarPillStyle = {
  alignItems: "center",
  background: "rgba(255, 255, 255, 0.84)",
  border: "1px solid var(--quartz-border)",
  borderRadius: 6,
  color: "var(--quartz-ink)",
  display: "inline-flex",
  fontSize: 11,
  fontWeight: 600,
  gap: 5,
  minHeight: 28,
  padding: "0 8px",
} satisfies CSSProperties;

const conversationToolbarIconStyle = {
  height: 12,
  width: 12,
} satisfies CSSProperties;

const conversationStatusPillStyle = {
  border: "1px solid rgba(69, 97, 123, 0.22)",
  borderRadius: 999,
  background: "rgba(69, 97, 123, 0.08)",
  color: "var(--quartz-secondary)",
  fontSize: 11,
  fontWeight: 600,
  padding: "3px 8px",
} satisfies CSSProperties;

const conversationMutedPillStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  color: "var(--quartz-muted)",
  fontSize: 11,
  fontWeight: 600,
  padding: "3px 8px",
} satisfies CSSProperties;

const conversationErrorStyle = {
  border: "1px solid rgba(123, 45, 38, 0.22)",
  borderRadius: 999,
  background: "rgba(255, 218, 214, 0.72)",
  color: "var(--quartz-error)",
  fontSize: 11,
  fontWeight: 600,
  lineHeight: "16px",
  padding: "3px 8px",
} satisfies CSSProperties;

const messageListStyle = {
  minHeight: 0,
  overflow: "auto",
  padding: "20px 24px 16px",
} satisfies CSSProperties;

const messageStreamStyle = {
  display: "grid",
  gap: 16,
  margin: "0 auto",
  maxWidth: 1080,
  width: "100%",
} satisfies CSSProperties;

const emptyConversationCardStyle = {
  alignSelf: "center",
  border: "1px solid var(--quartz-border)",
  borderRadius: 16,
  background: "rgba(255, 255, 255, 0.84)",
  display: "grid",
  gap: 8,
  justifySelf: "center",
  marginTop: 36,
  maxWidth: 600,
  padding: "24px 26px",
  textAlign: "center",
} satisfies CSSProperties;

const emptyConversationEyebrowStyle = {
  color: "var(--quartz-secondary)",
  fontSize: 10,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
} satisfies CSSProperties;

const emptyConversationTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 22,
  fontWeight: 600,
  letterSpacing: "-0.04em",
  margin: 0,
} satisfies CSSProperties;

const emptyConversationBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 13,
  lineHeight: "20px",
  margin: "0 auto",
  maxWidth: 480,
} satisfies CSSProperties;

const assistantMessageContainerStyle = {
  display: "flex",
  gap: 10,
  justifyContent: "flex-start",
} satisfies CSSProperties;

const userMessageContainerStyle = {
  display: "flex",
  justifyContent: "flex-end",
} satisfies CSSProperties;

const assistantAvatarStyle = {
  alignItems: "center",
  background: "var(--quartz-primary)",
  borderRadius: 999,
  color: "var(--quartz-primary-contrast)",
  display: "inline-flex",
  flex: "0 0 auto",
  height: 28,
  justifyContent: "center",
  marginTop: 12,
  width: 28,
} satisfies CSSProperties;

const assistantAvatarIconStyle = {
  height: 14,
  width: 14,
} satisfies CSSProperties;

const assistantMessageBubbleStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 10,
  background: "rgba(255, 255, 255, 0.92)",
  display: "grid",
  gap: 10,
  maxWidth: "min(100%, 1080px)",
  minWidth: 0,
  padding: "14px 16px 12px",
  width: "min(100%, 1080px)",
} satisfies CSSProperties;

const userMessageBubbleStyle = {
  border: "1px solid rgba(142, 115, 75, 0.22)",
  borderRadius: 10,
  background: "rgba(255, 251, 235, 0.72)",
  display: "grid",
  gap: 8,
  maxWidth: "min(100%, 600px)",
  minWidth: 0,
  padding: "10px 14px",
} satisfies CSSProperties;

const messageHeaderStyle = {
  alignItems: "center",
  display: "flex",
  gap: 8,
  justifyContent: "space-between",
} satisfies CSSProperties;

function messageRoleStyle(role: ChatMessageRecord["role"]) {
  return {
    color: role === "user" ? "var(--quartz-secondary)" : "var(--quartz-ink)",
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: "0.04em",
    textTransform: "uppercase",
  } satisfies CSSProperties;
}

const messageTimeStyle = {
  color: "var(--quartz-muted)",
  fontSize: 11,
} satisfies CSSProperties;

const messageContentStyle = {
  color: "var(--quartz-ink)",
  fontSize: 14,
  lineHeight: "22px",
  margin: 0,
  overflowWrap: "anywhere",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
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

const visuallyHiddenStyle = {
  border: 0,
  clip: "rect(0, 0, 0, 0)",
  height: 1,
  margin: -1,
  overflow: "hidden",
  padding: 0,
  position: "absolute",
  whiteSpace: "nowrap",
  width: 1,
} satisfies CSSProperties;
