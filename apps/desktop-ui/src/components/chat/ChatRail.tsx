/*
Purpose: Provide the grounded chat rail surface for the finance copilot experience.
Scope: Thread list, message history, message input, and read-only analysis response rendering
with evidence references. The chat rail is designed to sit alongside main content views
(e.g., close run detail, document review) as a collapsible right panel.
Dependencies: React, desktop UI chat API helpers, and shared design tokens.
*/

"use client";

import { useCallback, useEffect, useRef, useState, type CSSProperties, type FormEvent } from "react";
import {
  type ChatMessageRecord,
  type ChatThreadSummary,
  type GroundingContext,
  ChatApiError,
  createChatThread,
  getChatThread,
  listChatThreads,
  sendChatMessage,
} from "../../lib/chat";

export type ChatRailProps = {
  closeRunId?: string;
  entityId: string;
};

/**
 * Purpose: Render the full chat rail with thread list, message view, and composer.
 * Inputs: Entity and optional close run grounding context.
 * Outputs: Collapsible chat rail surface with thread management and message history.
 * Behavior: Fetches threads on mount, allows thread creation and selection,
 * and supports sending messages with read-only analysis responses.
 */
export function ChatRail({ closeRunId, entityId }: Readonly<ChatRailProps>) {
  const [threads, setThreads] = useState<ChatThreadSummary[]>([]);
  const [selectedThread, setSelectedThread] = useState<ChatThreadSummary | null>(null);
  const [messages, setMessages] = useState<ChatMessageRecord[]>([]);
  const [inputValue, setInputValue] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isExpanded, setIsExpanded] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadThreads().catch((err: unknown) => {
      console.error("Failed to load chat threads:", err);
    });
  }, [entityId, closeRunId]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const loadThreads = useCallback(async () => {
    try {
      const options: { closeRunId?: string; limit?: number } = {
        limit: 50,
      };
      if (closeRunId) {
        options.closeRunId = closeRunId;
      }
      const response = await listChatThreads(entityId, options);
      setThreads(response.threads);
    } catch (err: unknown) {
      if (err instanceof ChatApiError && err.status !== 401) {
        setError("Failed to load chat threads.");
      }
    }
  }, [entityId, closeRunId]);

  const handleSelectThread = useCallback(async (thread: ChatThreadSummary) => {
    setSelectedThread(thread);
    setError(null);
    try {
      const response = await getChatThread(thread.id, entityId);
      setMessages(response.messages);
    } catch (err: unknown) {
      if (err instanceof ChatApiError && err.status !== 401) {
        setError("Failed to load thread messages.");
      }
    }
  }, [entityId]);

  const handleCreateThread = useCallback(async () => {
    try {
      const request: {
        close_run_id?: string;
        entity_id: string;
        title?: string;
      } = {
        entity_id: entityId,
      };
      if (closeRunId) {
        request.close_run_id = closeRunId;
        request.title = "Close Run Discussion";
      }
      const response = await createChatThread(request);
      setThreads((prev) => [response.thread, ...prev]);
      setSelectedThread(response.thread);
      setMessages([]);
      setError(null);
    } catch (err: unknown) {
      const apiError = err instanceof ChatApiError ? err : null;
      if (apiError && apiError.status !== 401) {
        setError("Failed to create a new chat thread.");
      }
    }
  }, [entityId, closeRunId]);

  const handleSendMessage = useCallback(
    async (content: string) => {
      if (!selectedThread) {
        return;
      }

      setIsLoading(true);
      setError(null);

      const optimisticUserMessage: ChatMessageRecord = {
        content,
        created_at: new Date().toISOString(),
        grounding_payload: {},
        id: `optimistic-${Date.now()}`,
        linked_action_id: null,
        message_type: "analysis",
        model_metadata: null,
        role: "user",
        thread_id: selectedThread.id,
      };
      setMessages((prev) => [...prev, optimisticUserMessage]);
      setInputValue("");

      try {
        const response = await sendChatMessage(
          selectedThread.id,
          entityId,
          content,
        );
        setMessages((prev) => {
          const filtered = prev.filter((msg) => !msg.id.startsWith("optimistic-"));
          const newUserMessage = response.user_message ?? optimisticUserMessage;
          return [...filtered, newUserMessage, response.message];
        });
      } catch (err: unknown) {
        const apiError = err instanceof ChatApiError ? err : null;
        if (apiError && apiError.status !== 401) {
          setError(apiError.message ?? "Failed to get a response.");
        }
        setMessages((prev) => prev.filter((msg) => !msg.id.startsWith("optimistic-")));
      } finally {
        setIsLoading(false);
      }
    },
    [selectedThread, entityId],
  );

  const handleSubmit = useCallback(
    (event: FormEvent) => {
      event.preventDefault();
      const trimmed = inputValue.trim();
      if (!trimmed || isLoading || !selectedThread) {
        return;
      }
      void handleSendMessage(trimmed);
    },
    [inputValue, isLoading, selectedThread, handleSendMessage],
  );

  const grounding = selectedThread?.grounding;

  if (!isExpanded) {
    return (
      <button
        aria-label="Expand chat rail"
        onClick={() => setIsExpanded(true)}
        style={expandButtonStyle}
        type="button"
      >
        Chat
      </button>
    );
  }

  return (
    <aside aria-label="Chat rail" style={railContainerStyle}>
      <ChatRailHeader
        grounding={grounding ?? null}
        onCollapse={() => setIsExpanded(false)}
        threadCount={threads.length}
      />

      {selectedThread ? (
        <>
          <MessageList
            isLoading={isLoading}
            messages={messages}
          />
          <MessageComposer
            error={error}
            isLoading={isLoading}
            onSubmit={handleSubmit}
            placeholder="Ask about this period's documents, extractions, or recommendations..."
            value={inputValue}
            onChange={setInputValue}
          />
        </>
      ) : (
        <ThreadList
          threads={threads}
          onCreateThread={handleCreateThread}
          onSelectThread={handleSelectThread}
        />
      )}
      <div ref={messagesEndRef} />
    </aside>
  );
}

/* ------------------------------------------------------------------ */
/* Sub-components                                                      */
/* ------------------------------------------------------------------ */

type ChatRailHeaderProps = {
  grounding: GroundingContext | null;
  onCollapse: () => void;
  threadCount: number;
};

function ChatRailHeader({ grounding, onCollapse, threadCount }: Readonly<ChatRailHeaderProps>) {
  return (
    <header style={headerStyle}>
      <div style={{ display: "grid", gap: "2px" }}>
        <div style={{ alignItems: "center", display: "flex", gap: "8px" }}>
          <h3 style={headerTitleStyle}>Chat</h3>
          {grounding && (
            <span style={groundingBadgeStyle}>
              {grounding.entity_name}
              {grounding.period_label ? ` · ${grounding.period_label}` : ""}
            </span>
          )}
        </div>
        <p style={headerSubtitleStyle}>
          {threadCount} {threadCount === 1 ? "thread" : "threads"}
        </p>
      </div>
      <button
        aria-label="Collapse chat rail"
        onClick={onCollapse}
        style={collapseButtonStyle}
        type="button"
      >
        Collapse
      </button>
    </header>
  );
}

type ThreadListProps = {
  threads: ChatThreadSummary[];
  onCreateThread: () => void;
  onSelectThread: (thread: ChatThreadSummary) => void;
};

function ThreadList({ threads, onCreateThread, onSelectThread }: Readonly<ThreadListProps>) {
  return (
    <div style={threadListStyle}>
      <button
        onClick={onCreateThread}
        style={newThreadButtonStyle}
        type="button"
      >
        + New Thread
      </button>

      {threads.length === 0 ? (
        <p style={emptyTextStyle}>
          No chat threads yet. Create one to start a conversation grounded in this workspace.
        </p>
      ) : (
        <ul style={threadItemsStyle}>
          {threads.map((thread) => (
            <li key={thread.id}>
              <button
                onClick={() => onSelectThread(thread)}
                style={threadItemStyle}
                type="button"
              >
                <span style={threadTitleStyle}>
                  {thread.title ?? "Untitled Thread"}
                </span>
                <span style={threadMetaStyle}>
                  {thread.message_count} {thread.message_count === 1 ? "message" : "messages"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

type MessageListProps = {
  isLoading: boolean;
  messages: ChatMessageRecord[];
};

function MessageList({ isLoading, messages }: Readonly<MessageListProps>) {
  return (
    <div style={messageListStyle}>
      {messages.length === 0 && !isLoading ? (
        <p style={emptyTextStyle}>
          Start the conversation. Responses are grounded in current workflow state and evidence.
        </p>
      ) : (
        messages.map((message) => (
          <div
            key={message.id}
            style={
              message.role === "user" ? userMessageStyle : assistantMessageStyle
            }
          >
            <div style={messageHeaderStyle}>
              <span style={messageRoleStyle}>
                {message.role === "assistant" ? "Copilot" : "You"}
              </span>
              {message.message_type !== "analysis" && (
                <span style={messageTypeBadgeStyle}>
                  {message.message_type.replaceAll("_", " ")}
                </span>
              )}
            </div>
            <p style={messageContentStyle}>{message.content}</p>
          </div>
        ))
      )}

      {isLoading && (
        <div style={assistantMessageStyle}>
          <p style={{ color: "#B7C3D6", fontStyle: "italic", margin: 0 }}>
            Analyzing workflow state...
          </p>
        </div>
      )}
    </div>
  );
}

type MessageComposerProps = {
  error: string | null;
  isLoading: boolean;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
};

function MessageComposer({
  error,
  isLoading,
  placeholder,
  value,
  onChange,
  onSubmit,
}: Readonly<MessageComposerProps>) {
  return (
    <form onSubmit={onSubmit} style={composerStyle}>
      {error && <p style={errorStyle}>{error}</p>}
      <div style={inputRowStyle}>
        <input
          disabled={isLoading}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          value={value}
          style={inputStyle}
        />
        <button
          disabled={isLoading || !value.trim()}
          style={sendButtonStyle}
          type="submit"
        >
          {isLoading ? "..." : "Send"}
        </button>
      </div>
    </form>
  );
}

/* ------------------------------------------------------------------ */
/* Inline styles (SPEC: dark enterprise theme)                        */
/* ------------------------------------------------------------------ */

const railContainerStyle: CSSProperties = {
  background: "#121A2B",
  borderLeft: "1px solid #24324A",
  display: "flex",
  flexDirection: "column",
  height: "100%",
  width: "360px",
};

const expandButtonStyle: CSSProperties = {
  background: "#4C8BF5",
  border: "none",
  borderRadius: "8px",
  color: "#F4F7FB",
  cursor: "pointer",
  fontWeight: 600,
  padding: "8px 16px",
  position: "fixed",
  right: "16px",
  bottom: "16px",
  zIndex: 50,
};

const headerStyle: CSSProperties = {
  alignItems: "center",
  borderBottom: "1px solid #24324A",
  display: "flex",
  justifyContent: "space-between",
  padding: "12px 16px",
};

const headerTitleStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: "16px",
  fontWeight: 600,
  margin: 0,
};

const headerSubtitleStyle: CSSProperties = {
  color: "#B7C3D6",
  fontSize: "11px",
  fontWeight: 500,
  margin: 0,
};

const groundingBadgeStyle: CSSProperties = {
  background: "rgba(76, 139, 245, 0.15)",
  borderRadius: "999px",
  color: "#4C8BF5",
  fontSize: "11px",
  fontWeight: 500,
  padding: "2px 8px",
};

const collapseButtonStyle: CSSProperties = {
  appearance: "none",
  background: "rgba(255, 255, 255, 0.06)",
  border: "1px solid #24324A",
  borderRadius: "8px",
  color: "#B7C3D6",
  cursor: "pointer",
  fontSize: "12px",
  fontWeight: 600,
  padding: "4px 10px",
};

const threadListStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  flex: 1,
  overflow: "auto",
  padding: "12px",
};

const newThreadButtonStyle: CSSProperties = {
  background: "#4C8BF5",
  border: "none",
  borderRadius: "8px",
  color: "#F4F7FB",
  cursor: "pointer",
  fontWeight: 600,
  marginBottom: "12px",
  padding: "8px 16px",
};

const emptyTextStyle: CSSProperties = {
  color: "#B7C3D6",
  fontSize: "13px",
  lineHeight: "20px",
  margin: 0,
  textAlign: "center",
};

const threadItemsStyle: CSSProperties = {
  display: "grid",
  gap: "6px",
  listStyle: "none",
  margin: 0,
  padding: 0,
};

const threadItemStyle: CSSProperties = {
  background: "#182338",
  border: "1px solid #24324A",
  borderRadius: "8px",
  cursor: "pointer",
  display: "flex",
  flexDirection: "column",
  gap: "4px",
  padding: "10px 12px",
  textAlign: "left" as const,
  width: "100%",
};

const threadTitleStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: "13px",
  fontWeight: 500,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap" as const,
};

const threadMetaStyle: CSSProperties = {
  color: "#B7C3D6",
  fontSize: "11px",
};

const messageListStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  flex: 1,
  gap: "10px",
  overflow: "auto",
  padding: "12px 16px",
};

const userMessageStyle: CSSProperties = {
  background: "#1A2D4A",
  border: "1px solid #24324A",
  borderRadius: "10px",
  padding: "10px 12px",
};

const assistantMessageStyle: CSSProperties = {
  background: "#182338",
  border: "1px solid #24324A",
  borderRadius: "10px",
  padding: "10px 12px",
};

const messageHeaderStyle: CSSProperties = {
  alignItems: "center",
  display: "flex",
  gap: "8px",
  marginBottom: "6px",
};

const messageRoleStyle: CSSProperties = {
  color: "#4C8BF5",
  fontSize: "11px",
  fontWeight: 600,
  textTransform: "uppercase" as const,
};

const messageTypeBadgeStyle: CSSProperties = {
  background: "rgba(231, 169, 59, 0.15)",
  borderRadius: "999px",
  color: "#E7A93B",
  fontSize: "10px",
  fontWeight: 500,
  padding: "1px 6px",
};

const messageContentStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: "13px",
  lineHeight: "20px",
  margin: 0,
  whiteSpace: "pre-wrap" as const,
};

const composerStyle: CSSProperties = {
  borderTop: "1px solid #24324A",
  display: "flex",
  flexDirection: "column",
  gap: "6px",
  padding: "12px 16px",
};

const errorStyle: CSSProperties = {
  color: "#D9534F",
  fontSize: "12px",
  margin: 0,
};

const inputRowStyle: CSSProperties = {
  display: "flex",
  gap: "8px",
};

const inputStyle: CSSProperties = {
  background: "#0B1020",
  border: "1px solid #24324A",
  borderRadius: "8px",
  color: "#F4F7FB",
  flex: 1,
  fontSize: "13px",
  padding: "8px 12px",
};

const sendButtonStyle: CSSProperties = {
  appearance: "none",
  background: "#4C8BF5",
  border: "none",
  borderRadius: "8px",
  color: "#F4F7FB",
  cursor: "pointer",
  fontWeight: 600,
  padding: "8px 16px",
};
