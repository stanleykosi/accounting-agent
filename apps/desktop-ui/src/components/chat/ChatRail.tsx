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
import { ActionComposer, type ComposerDraft } from "./ActionComposer";
import {
  ChatApiError,
  createChatThread,
  deleteChatThread,
  getChatThread,
  getChatThreadWorkspace,
  listChatThreads,
  type ChatActionResponse,
  type ChatMessageRecord,
  type ChatThreadSummary,
  type ChatThreadWorkspace,
} from "../../lib/chat";

export type ChatRailProps = {
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

export function ChatRail({
  closeRunId,
  entityId,
  presentation = "rail",
}: Readonly<ChatRailProps>): ReactElement {
  const [threads, setThreads] = useState<ChatThreadSummary[]>([]);
  const [selectedThread, setSelectedThread] = useState<ChatThreadSummary | null>(null);
  const [messages, setMessages] = useState<ChatMessageRecord[]>([]);
  const [workspace, setWorkspace] = useState<ChatThreadWorkspace | null>(null);
  const [pendingTurn, setPendingTurn] = useState<PendingTurn | null>(null);
  const [deletingThreadId, setDeletingThreadId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isBootstrapping, setIsBootstrapping] = useState(true);
  const [isLoadingThread, setIsLoadingThread] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const loadThreads = useCallback(async (): Promise<ChatThreadSummary[]> => {
    const response = await listChatThreads(
      entityId,
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
    return response.threads;
  }, [closeRunId, entityId]);

  const loadThreadWorkspace = useCallback(
    async (
      thread: ChatThreadSummary,
      options?: {
        showLoader?: boolean;
      },
    ): Promise<void> => {
      const showLoader = options?.showLoader ?? true;
      setSelectedThread(thread);
      if (showLoader) {
        setIsLoadingThread(true);
      }
      setError(null);

      try {
        const [threadDetail, threadWorkspace] = await Promise.all([
          getChatThread(thread.id, entityId),
          getChatThreadWorkspace(thread.id, entityId),
        ]);
        setMessages(threadDetail.messages);
        setWorkspace(threadWorkspace);
        setPendingTurn(null);
      } catch (error: unknown) {
        if (error instanceof ChatApiError && error.status !== 401) {
          setError("The selected chat could not be loaded.");
        }
      } finally {
        if (showLoader) {
          setIsLoadingThread(false);
        }
      }
    },
    [entityId],
  );

  const handleCreateThread = useCallback(async (): Promise<ChatThreadSummary | null> => {
    try {
      const response = await createChatThread(
        closeRunId
          ? {
              close_run_id: closeRunId,
              entity_id: entityId,
            }
          : {
              entity_id: entityId,
            },
      );
      const nextThread = response.thread;
      setThreads((current) => dedupeThreads([nextThread, ...current]));
      await loadThreadWorkspace(nextThread, { showLoader: false });
      setError(null);
      return nextThread;
    } catch (error: unknown) {
      if (error instanceof ChatApiError && error.status !== 401) {
        setError("A new chat could not be created.");
      }
      return null;
    }
  }, [closeRunId, entityId, loadThreadWorkspace]);

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
          await loadThreadWorkspace(loadedThreads[0], { showLoader: false });
          return;
        }

        if (presentation === "workspace") {
          await handleCreateThread();
        }
      } catch (error: unknown) {
        if (error instanceof ChatApiError && error.status !== 401) {
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
  }, [handleCreateThread, loadThreadWorkspace, loadThreads, presentation]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pendingTurn, isLoadingThread]);

  const refreshSelectedThread = useCallback(async (): Promise<void> => {
    if (selectedThread === null) {
      return;
    }

    await loadThreadWorkspace(selectedThread, { showLoader: false });
    try {
      await loadThreads();
    } catch {
      // Keep the current thread visible even if the thread list refresh fails.
    }
  }, [loadThreadWorkspace, loadThreads, selectedThread]);

  const handleDeleteThread = useCallback(
    async (thread: ChatThreadSummary): Promise<void> => {
      const confirmed = window.confirm(
        `Delete "${formatThreadTitle(thread)}"? This removes the full conversation history.`,
      );
      if (!confirmed) {
        return;
      }

      setDeletingThreadId(thread.id);
      try {
        await deleteChatThread(thread.id, entityId);
        const remainingThreads = threads.filter((candidate) => candidate.id !== thread.id);
        setThreads(remainingThreads);
        setError(null);

        if (selectedThread?.id !== thread.id) {
          return;
        }

        if (remainingThreads[0] !== undefined) {
          await loadThreadWorkspace(remainingThreads[0], { showLoader: false });
          return;
        }

        setMessages([]);
        setWorkspace(null);
        setSelectedThread(null);
        setPendingTurn(null);

        if (presentation === "workspace") {
          await handleCreateThread();
        }
      } catch (error: unknown) {
        if (error instanceof ChatApiError && error.status !== 401) {
          setError("The selected chat could not be deleted.");
        }
      } finally {
        setDeletingThreadId(null);
      }
    },
    [entityId, handleCreateThread, loadThreadWorkspace, presentation, selectedThread, threads],
  );

  const renderableMessages = useMemo(
    () => buildRenderableMessages(messages),
    [messages],
  );

  const isAwaitingReply = pendingTurn !== null && pendingTurn.assistantContent === null;

  return (
    <div
      style={
        presentation === "workspace"
          ? workbenchShellStyle
          : { ...workbenchShellStyle, gridTemplateColumns: "minmax(240px, 280px) minmax(0, 1fr)" }
      }
    >
      <ThreadSidebar
        deletingThreadId={deletingThreadId}
        isBootstrapping={isBootstrapping}
        selectedThreadId={selectedThread?.id ?? null}
        threads={threads}
        onCreateThread={() => {
          void handleCreateThread();
        }}
        onDeleteThread={(thread) => {
          void handleDeleteThread(thread);
        }}
        onSelectThread={(thread) => {
          void loadThreadWorkspace(thread);
        }}
      />

      <section style={conversationPaneStyle}>
        <ConversationHeader
          error={error}
          isLoading={isLoadingThread || isBootstrapping}
          thread={selectedThread}
        />

        <MessageList
          isAwaitingReply={isAwaitingReply}
          isLoading={isLoadingThread || isBootstrapping}
          messages={renderableMessages}
          pendingTurn={pendingTurn}
        />

        <ActionComposer
          closeRunId={selectedThread?.close_run_id ?? closeRunId}
          disabled={isLoadingThread || isBootstrapping || selectedThread === null}
          entityId={entityId}
          onActionStateChange={() => {
            void refreshSelectedThread();
          }}
          onMessageSent={(response: ChatActionResponse, draft: ComposerDraft) => {
            setPendingTurn({
              assistantContent: response.content,
              draft,
            });
            void refreshSelectedThread();
          }}
          onSubmissionError={() => {
            setPendingTurn(null);
          }}
          onSubmissionStart={(draft: ComposerDraft) => {
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
  );
}

type ThreadSidebarProps = {
  deletingThreadId: string | null;
  isBootstrapping: boolean;
  onCreateThread: () => void;
  onDeleteThread: (thread: ChatThreadSummary) => void;
  onSelectThread: (thread: ChatThreadSummary) => void;
  selectedThreadId: string | null;
  threads: readonly ChatThreadSummary[];
};

function ThreadSidebar({
  deletingThreadId,
  isBootstrapping,
  onCreateThread,
  onDeleteThread,
  onSelectThread,
  selectedThreadId,
  threads,
}: Readonly<ThreadSidebarProps>): ReactElement {
  return (
    <aside style={threadSidebarStyle}>
      <div style={threadSidebarHeaderStyle}>
        <div style={{ display: "grid", gap: 6 }}>
          <p style={sidebarEyebrowStyle}>Assistant</p>
          <h2 style={sidebarTitleStyle}>Chats</h2>
          <p style={sidebarBodyStyle}>
            One clean workspace for questions, approvals, and source-document uploads.
          </p>
        </div>
        <button onClick={onCreateThread} style={newChatButtonStyle} type="button">
          New chat
        </button>
      </div>

      {isBootstrapping ? (
        <div style={emptySidebarCardStyle}>
          <p style={emptySidebarTitleStyle}>Preparing assistant…</p>
          <p style={emptySidebarBodyStyle}>Loading the latest chat workspace.</p>
        </div>
      ) : threads.length === 0 ? (
        <div style={emptySidebarCardStyle}>
          <p style={emptySidebarTitleStyle}>No chats yet</p>
          <p style={emptySidebarBodyStyle}>
            A new chat will open automatically when you begin working here.
          </p>
        </div>
      ) : (
        <ul style={threadListStyle}>
          {threads.map((thread) => {
            const isActive = thread.id === selectedThreadId;
            return (
              <li key={thread.id}>
                <div style={threadRowStyle}>
                  <button
                    onClick={() => onSelectThread(thread)}
                    style={isActive ? { ...threadCardStyle, ...threadCardActiveStyle } : threadCardStyle}
                    type="button"
                  >
                    <div style={{ display: "grid", gap: 6 }}>
                      <span style={threadTitleStyle}>{formatThreadTitle(thread)}</span>
                      <span style={threadMetaStyle}>
                        {formatThreadSubtitle(thread)} · {formatThreadTime(thread.updated_at)}
                      </span>
                    </div>
                  </button>
                  <button
                    aria-label={`Delete ${formatThreadTitle(thread)}`}
                    disabled={deletingThreadId === thread.id}
                    onClick={() => onDeleteThread(thread)}
                    style={threadDeleteButtonStyle(deletingThreadId === thread.id)}
                    title="Delete chat"
                    type="button"
                  >
                    {deletingThreadId === thread.id ? "…" : "×"}
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
  error: string | null;
  isLoading: boolean;
  thread: ChatThreadSummary | null;
};

function ConversationHeader({
  error,
  isLoading,
  thread,
}: Readonly<ConversationHeaderProps>): ReactElement {
  return (
    <header style={conversationHeaderStyle}>
      <div style={{ display: "grid", gap: 6 }}>
        <p style={conversationEyebrowStyle}>Assistant Workspace</p>
        <div style={conversationTitleRowStyle}>
          <h2 style={conversationTitleStyle}>
            {thread === null ? "New chat" : formatThreadTitle(thread)}
          </h2>
          {thread !== null ? (
            <span style={conversationMetaPillStyle}>
              {thread.grounding.period_label ?? "Entity scope"}
            </span>
          ) : null}
        </div>
        <p style={conversationBodyStyle}>
          Ask questions naturally, upload source documents, and continue the close from one clean
          conversation. Hidden workflow context stays grounded in the backend and out of the UI.
        </p>
      </div>
      {error ? (
        <div style={conversationErrorStyle} role="status">
          {error}
        </div>
      ) : null}
      {isLoading ? (
        <div style={conversationLoadingStyle}>Refreshing the latest assistant state…</div>
      ) : null}
    </header>
  );
}

type MessageListProps = {
  isAwaitingReply: boolean;
  isLoading: boolean;
  messages: readonly RenderableMessage[];
  pendingTurn: PendingTurn | null;
};

function MessageList({
  isAwaitingReply,
  isLoading,
  messages,
  pendingTurn,
}: Readonly<MessageListProps>): ReactElement {
  const hasMessages = messages.length > 0 || pendingTurn !== null;

  return (
    <div style={messageListStyle}>
      {!hasMessages && !isLoading ? (
        <div style={emptyConversationCardStyle}>
          <p style={emptyConversationEyebrowStyle}>Ready</p>
          <h3 style={emptyConversationTitleStyle}>Start the conversation</h3>
          <p style={emptyConversationBodyStyle}>
            Use the assistant like a modern chat workspace. Ask for the next step, resolve a
            blocker, or upload source documents and let the agent stay grounded to this close.
          </p>
        </div>
      ) : null}

      {messages.map((message) => (
        <article
          key={message.id}
          style={message.role === "user" ? userMessageContainerStyle : assistantMessageContainerStyle}
        >
          <div style={message.role === "user" ? userMessageBubbleStyle : assistantMessageBubbleStyle}>
            <div style={messageHeaderStyle}>
              <span style={messageRoleStyle(message.role)}>{message.role === "user" ? "You" : "Assistant"}</span>
              <span style={messageTimeStyle}>{message.displayTime}</span>
            </div>
            {extractInlineAttachments(message).length > 0 ? (
              <div style={inlineAttachmentRowStyle}>
                {extractInlineAttachments(message).map((attachment) => (
                  <span key={`${message.id}-${attachment.filename}`} style={inlineAttachmentPillStyle}>
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
                <span style={messageTimeStyle}>Sending…</span>
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
                <span style={messageTimeStyle}>{isAwaitingReply ? "Thinking…" : "Reply ready"}</span>
              </div>
              {pendingTurn.assistantContent === null ? (
                <div style={thinkingBubbleStyle}>
                  <span className="quartz-chat-thinking-dots">
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
  );
}

function buildRenderableMessages(messages: readonly ChatMessageRecord[]): RenderableMessage[] {
  return messages
    .map((message, index) => ({ index, message }))
    .filter(({ message }) => message.role !== "system" && !looksLikeInternalContextDump(message.content))
    .sort((left, right) => {
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
  const messageCountLabel = thread.message_count === 1 ? "1 message" : `${thread.message_count} messages`;
  return `${scope} · ${messageCountLabel}`;
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

const workbenchShellStyle = {
  display: "grid",
  gridTemplateColumns: "minmax(250px, 292px) minmax(0, 1fr)",
  height: "100%",
  minHeight: 0,
  overflow: "hidden",
} satisfies CSSProperties;

const threadSidebarStyle = {
  background: "linear-gradient(180deg, rgba(241, 237, 236, 0.82) 0%, rgba(247, 243, 242, 0.96) 100%)",
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
  fontSize: 28,
  fontWeight: 600,
  letterSpacing: "-0.05em",
  margin: 0,
} satisfies CSSProperties;

const sidebarBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
} satisfies CSSProperties;

const newChatButtonStyle = {
  border: "1px solid var(--quartz-primary)",
  borderRadius: 999,
  background: "var(--quartz-primary)",
  color: "var(--quartz-primary-contrast)",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 700,
  minHeight: 38,
  padding: "0 16px",
} satisfies CSSProperties;

const emptySidebarCardStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 18,
  background: "var(--quartz-surface)",
  display: "grid",
  gap: 8,
  padding: 18,
} satisfies CSSProperties;

const emptySidebarTitleStyle = {
  color: "var(--quartz-ink)",
  fontSize: 16,
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

const threadCardStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 16,
  background: "rgba(255, 255, 255, 0.82)",
  color: "var(--quartz-ink)",
  cursor: "pointer",
  padding: "14px 16px",
  textAlign: "left",
  width: "100%",
} satisfies CSSProperties;

const threadCardActiveStyle = {
  borderColor: "rgba(69, 97, 123, 0.28)",
  background: "rgba(69, 97, 123, 0.1)",
  boxShadow: "inset 3px 0 0 var(--quartz-secondary)",
} satisfies CSSProperties;

const threadTitleStyle = {
  color: "var(--quartz-ink)",
  fontSize: 14,
  fontWeight: 600,
} satisfies CSSProperties;

const threadMetaStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
  lineHeight: "18px",
} satisfies CSSProperties;

function threadDeleteButtonStyle(disabled: boolean) {
  return {
    alignItems: "center",
    border: "1px solid rgba(123, 45, 38, 0.2)",
    borderRadius: 14,
    background: "rgba(255, 218, 214, 0.62)",
    color: "var(--quartz-error)",
    cursor: disabled ? "not-allowed" : "pointer",
    display: "inline-flex",
    fontSize: 20,
    justifyContent: "center",
    minWidth: 40,
    opacity: disabled ? 0.65 : 1,
  } satisfies CSSProperties;
}

const conversationPaneStyle = {
  background: "var(--quartz-paper)",
  display: "flex",
  flexDirection: "column",
  minHeight: 0,
} satisfies CSSProperties;

const conversationHeaderStyle = {
  borderBottom: "1px solid var(--quartz-border)",
  display: "grid",
  gap: 10,
  padding: "24px 28px 18px",
} satisfies CSSProperties;

const conversationEyebrowStyle = {
  color: "var(--quartz-secondary)",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
} satisfies CSSProperties;

const conversationTitleRowStyle = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 10,
} satisfies CSSProperties;

const conversationTitleStyle = {
  color: "var(--quartz-ink)",
  fontFamily: "var(--font-display)",
  fontSize: 34,
  fontWeight: 600,
  letterSpacing: "-0.06em",
  lineHeight: 1.02,
  margin: 0,
} satisfies CSSProperties;

const conversationMetaPillStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: 999,
  color: "var(--quartz-muted)",
  fontSize: 12,
  fontWeight: 600,
  padding: "6px 10px",
} satisfies CSSProperties;

const conversationBodyStyle = {
  color: "var(--quartz-muted)",
  fontSize: 14,
  lineHeight: "22px",
  margin: 0,
  maxWidth: 820,
} satisfies CSSProperties;

const conversationErrorStyle = {
  border: "1px solid rgba(123, 45, 38, 0.22)",
  borderRadius: 12,
  background: "rgba(255, 218, 214, 0.72)",
  color: "var(--quartz-error)",
  fontSize: 12,
  lineHeight: "18px",
  padding: "10px 12px",
} satisfies CSSProperties;

const conversationLoadingStyle = {
  color: "var(--quartz-muted)",
  fontSize: 12,
  fontWeight: 600,
} satisfies CSSProperties;

const messageListStyle = {
  display: "flex",
  flex: 1,
  flexDirection: "column",
  gap: 18,
  minHeight: 0,
  overflow: "auto",
  padding: "28px",
} satisfies CSSProperties;

const emptyConversationCardStyle = {
  alignSelf: "center",
  border: "1px solid var(--quartz-border)",
  borderRadius: 22,
  background: "rgba(255, 255, 255, 0.86)",
  display: "grid",
  gap: 10,
  marginTop: 28,
  maxWidth: 560,
  padding: "26px 28px",
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
} satisfies CSSProperties;

const assistantMessageContainerStyle = {
  alignItems: "flex-start",
  display: "flex",
  justifyContent: "flex-start",
} satisfies CSSProperties;

const userMessageContainerStyle = {
  alignItems: "flex-end",
  display: "flex",
  justifyContent: "flex-end",
} satisfies CSSProperties;

const assistantMessageBubbleStyle = {
  border: "1px solid var(--quartz-border)",
  borderRadius: "22px 22px 22px 8px",
  background: "rgba(255, 255, 255, 0.88)",
  display: "grid",
  gap: 10,
  maxWidth: 860,
  padding: "16px 18px",
  width: "min(100%, 860px)",
} satisfies CSSProperties;

const userMessageBubbleStyle = {
  border: "1px solid rgba(69, 97, 123, 0.22)",
  borderRadius: "22px 22px 8px 22px",
  background: "rgba(69, 97, 123, 0.08)",
  display: "grid",
  gap: 10,
  maxWidth: 720,
  padding: "16px 18px",
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
  lineHeight: "24px",
  margin: 0,
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
