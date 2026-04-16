/*
Purpose: Provide a compact embedded rail and a full accounting-agent workbench
surface for grounded chat operations.
Scope: Thread list, message history, action-capable composer, agent memory,
registered tools, and recent trace visibility.
Dependencies: React and the same-origin chat API helpers.
*/

"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { ActionComposer } from "./ActionComposer";
import {
  extractToolSchemaFields,
  summarizeToolSchema,
  type AgentTraceRecord,
  type AgentToolManifestItem,
  type ChatToolManifest,
  type ChatMessageRecord,
  type ChatThreadSummary,
  type ChatThreadWorkspace,
  type GroundingContext,
  type ToolSchemaField,
  ChatApiError,
  createChatThread,
  getChatThread,
  getChatThreadWorkspace,
  listChatThreads,
} from "../../lib/chat";
import { AgentReadinessPanel } from "./AgentReadinessPanel";

export type ChatRailProps = {
  closeRunId?: string;
  entityId: string;
  presentation?: "rail" | "workspace";
};

export function ChatRail({
  closeRunId,
  entityId,
  presentation = "rail",
}: Readonly<ChatRailProps>) {
  const [threads, setThreads] = useState<ChatThreadSummary[]>([]);
  const [selectedThread, setSelectedThread] = useState<ChatThreadSummary | null>(null);
  const [messages, setMessages] = useState<ChatMessageRecord[]>([]);
  const [workspace, setWorkspace] = useState<ChatThreadWorkspace | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isExpanded, setIsExpanded] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const loadThreads = useCallback(async (): Promise<ChatThreadSummary[]> => {
    const options: { closeRunId?: string; limit?: number } = { limit: 50 };
    if (closeRunId) {
      options.closeRunId = closeRunId;
    }
    const response = await listChatThreads(entityId, options);
    setThreads(response.threads);
    return response.threads;
  }, [closeRunId, entityId]);

  const loadThreadWorkspace = useCallback(
    async (thread: ChatThreadSummary): Promise<void> => {
      setSelectedThread(thread);
      setIsLoading(true);
      setError(null);
      try {
        const [threadDetail, threadWorkspace] = await Promise.all([
          getChatThread(thread.id, entityId),
          getChatThreadWorkspace(thread.id, entityId),
        ]);
        setMessages(threadDetail.messages);
        setWorkspace(threadWorkspace);
      } catch (err: unknown) {
        if (err instanceof ChatApiError && err.status !== 401) {
          setError("Failed to load the selected agent thread.");
        }
      } finally {
        setIsLoading(false);
      }
    },
    [entityId],
  );

  useEffect(() => {
    void (async () => {
      try {
        const loadedThreads = await loadThreads();
        const firstThread = loadedThreads[0];
        if (presentation === "workspace" && firstThread !== undefined && selectedThread === null) {
          await loadThreadWorkspace(firstThread);
        }
      } catch (err: unknown) {
        if (err instanceof ChatApiError && err.status !== 401) {
          setError("Failed to load chat threads.");
        }
      }
    })();
  }, [loadThreads, loadThreadWorkspace, presentation, selectedThread]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const refreshSelectedThread = useCallback(async (): Promise<void> => {
    if (selectedThread === null) {
      return;
    }
    await loadThreadWorkspace(selectedThread);
    try {
      await loadThreads();
    } catch {
      // Keep the current thread visible even if the thread list refresh fails.
    }
  }, [loadThreadWorkspace, loadThreads, selectedThread]);

  const handleCreateThread = useCallback(async () => {
    try {
      const request: { close_run_id?: string; entity_id: string; title?: string } = {
        entity_id: entityId,
      };
      if (closeRunId) {
        request.close_run_id = closeRunId;
        request.title = "Accounting agent workspace";
      }
      const response = await createChatThread(request);
      setThreads((prev) => [response.thread, ...prev]);
      await loadThreadWorkspace(response.thread);
      setError(null);
    } catch (err: unknown) {
      const apiError = err instanceof ChatApiError ? err : null;
      if (apiError && apiError.status !== 401) {
        setError("Failed to create a new chat thread.");
      }
    }
  }, [closeRunId, entityId, loadThreadWorkspace]);

  const grounding = selectedThread?.grounding ?? workspace?.grounding ?? null;
  const panel = presentation === "workspace" ? (
    <div style={workbenchShellStyle}>
      <ThreadSidebar
        threads={threads}
        selectedThreadId={selectedThread?.id ?? null}
        onCreateThread={() => {
          void handleCreateThread();
        }}
        onSelectThread={(thread) => {
          void loadThreadWorkspace(thread);
        }}
      />
      <section style={conversationPaneStyle}>
        <ConversationHeader
          error={error}
          grounding={grounding}
          memorySummary={workspace?.memory.progress_summary ?? null}
        />
        <MessageList isLoading={isLoading} messages={messages} />
        <ActionComposer
          closeRunId={selectedThread?.close_run_id ?? closeRunId}
          disabled={isLoading || selectedThread === null}
          entityId={entityId}
          onActionStateChange={() => {
            void refreshSelectedThread();
          }}
          onMessageSent={() => {
            void refreshSelectedThread();
          }}
          threadId={selectedThread?.id ?? ""}
          workspace={workspace}
        />
        <div ref={messagesEndRef} />
      </section>
      <AgentWorkspacePanel
        closeRunId={closeRunId}
        entityId={entityId}
        onRefresh={() => {
          void refreshSelectedThread();
        }}
        workspace={workspace}
      />
    </div>
  ) : (
    <CompactRail
      closeRunId={closeRunId}
      entityId={entityId}
      error={error}
      grounding={grounding}
      isExpanded={isExpanded}
      isLoading={isLoading}
      messages={messages}
      onCollapse={() => setIsExpanded(false)}
      onCreateThread={() => {
        void handleCreateThread();
      }}
      onExpand={() => setIsExpanded(true)}
      onRefresh={() => {
        void refreshSelectedThread();
      }}
      onSelectThread={(thread) => {
        void loadThreadWorkspace(thread);
      }}
      selectedThread={selectedThread}
      threads={threads}
      threadId={selectedThread?.id ?? ""}
      workspace={workspace}
    />
  );

  return panel;
}

type CompactRailProps = {
  closeRunId: string | undefined;
  entityId: string;
  error: string | null;
  grounding: GroundingContext | null;
  isExpanded: boolean;
  isLoading: boolean;
  messages: ChatMessageRecord[];
  onCollapse: () => void;
  onCreateThread: () => void;
  onExpand: () => void;
  onRefresh: () => void;
  onSelectThread: (thread: ChatThreadSummary) => void;
  selectedThread: ChatThreadSummary | null;
  threadId: string;
  threads: ChatThreadSummary[];
  workspace: ChatThreadWorkspace | null;
};

function CompactRail({
  closeRunId,
  entityId,
  error,
  grounding,
  isExpanded,
  isLoading,
  messages,
  onCollapse,
  onCreateThread,
  onExpand,
  onRefresh,
  onSelectThread,
  selectedThread,
  threadId,
  threads,
  workspace,
}: Readonly<CompactRailProps>) {
  if (!isExpanded) {
    return (
      <button aria-label="Expand chat rail" onClick={onExpand} style={expandButtonStyle} type="button">
        Agent
      </button>
    );
  }

  return (
    <aside aria-label="Chat rail" style={railContainerStyle}>
      <header style={railHeaderStyle}>
        <div style={{ display: "grid", gap: 2 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h3 style={railTitleStyle}>Accounting Agent</h3>
            {grounding ? (
              <span style={groundingBadgeStyle}>
                {grounding.entity_name}
                {grounding.period_label ? ` · ${grounding.period_label}` : ""}
              </span>
            ) : null}
          </div>
          <p style={railSubtitleStyle}>
            {threads.length} {threads.length === 1 ? "thread" : "threads"}
          </p>
        </div>
        <button aria-label="Collapse chat rail" onClick={onCollapse} style={ghostButtonStyle} type="button">
          Collapse
        </button>
      </header>

      {selectedThread === null ? (
        <ThreadSidebar
          threads={threads}
          selectedThreadId={null}
          onCreateThread={onCreateThread}
          onSelectThread={onSelectThread}
        />
      ) : (
        <>
          {error ? <StatusBanner tone="danger">{error}</StatusBanner> : null}
          <MessageList isLoading={isLoading} messages={messages} />
          <ActionComposer
            closeRunId={selectedThread?.close_run_id ?? closeRunId}
            disabled={isLoading}
            entityId={entityId}
            onActionStateChange={() => {
              void onRefresh();
            }}
            onMessageSent={() => {
              void onRefresh();
            }}
            threadId={threadId}
            workspace={workspace}
          />
        </>
      )}
    </aside>
  );
}

type ThreadSidebarProps = {
  onCreateThread: () => void;
  onSelectThread: (thread: ChatThreadSummary) => void;
  selectedThreadId: string | null;
  threads: ChatThreadSummary[];
};

function ThreadSidebar({
  onCreateThread,
  onSelectThread,
  selectedThreadId,
  threads,
}: Readonly<ThreadSidebarProps>) {
  return (
    <section style={threadSidebarStyle}>
      <div style={threadSidebarHeaderStyle}>
        <div>
          <p style={panelEyebrowStyle}>Threads</p>
          <h2 style={panelTitleStyle}>Operator sessions</h2>
        </div>
        <button onClick={onCreateThread} style={primaryButtonStyle} type="button">
          New thread
        </button>
      </div>

      {threads.length === 0 ? (
        <div style={emptyStateCardStyle}>
          <p style={emptyTitleStyle}>No agent threads yet</p>
          <p style={emptyBodyStyle}>
            Open a thread to inspect close progress, ask questions, and route deterministic actions.
          </p>
        </div>
      ) : (
        <ul style={threadListStyle}>
          {threads.map((thread) => (
            <li key={thread.id}>
              <button
                onClick={() => onSelectThread(thread)}
                style={
                  thread.id === selectedThreadId
                    ? { ...threadItemStyle, ...threadItemActiveStyle }
                    : threadItemStyle
                }
                type="button"
              >
                <div style={{ display: "grid", gap: 4 }}>
                  <span style={threadTitleStyle}>{thread.title ?? "Untitled thread"}</span>
                  <span style={threadMetaStyle}>
                    {thread.grounding.period_label ?? "Workspace scope"} · {thread.message_count} messages
                  </span>
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

type ConversationHeaderProps = {
  error: string | null;
  grounding: GroundingContext | null;
  memorySummary: string | null;
};

function ConversationHeader({
  error,
  grounding,
  memorySummary,
}: Readonly<ConversationHeaderProps>) {
  return (
    <header style={conversationHeaderStyle}>
      <div style={{ display: "grid", gap: 6 }}>
        <p style={panelEyebrowStyle}>Conversation</p>
        <h2 style={panelTitleStyle}>Agent workspace</h2>
        <p style={conversationBodyStyle}>
          {memorySummary ??
            "Use natural language to inspect workflow state, upload files, trigger actions, review approvals, and continue the close from here."}
        </p>
      </div>
      {grounding ? (
        <div style={conversationBadgeRowStyle}>
          <span style={groundingBadgeStyle}>{grounding.entity_name}</span>
          {grounding.period_label ? <span style={metaPillStyle}>{grounding.period_label}</span> : null}
          <span style={metaPillStyle}>{grounding.autonomy_mode.replaceAll("_", " ")}</span>
        </div>
      ) : null}
      {error ? <StatusBanner tone="danger">{error}</StatusBanner> : null}
    </header>
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
        <div style={emptyStateCardStyle}>
          <p style={emptyTitleStyle}>Start with an instruction</p>
          <p style={emptyBodyStyle}>
            Ask about status, approvals, reporting, exports, or next steps. You can also attach files and continue the workflow from chat.
          </p>
        </div>
      ) : null}
      {messages.map((message) => (
        <article
          key={message.id}
          style={message.role === "user" ? userMessageStyle : assistantMessageStyle}
        >
          <div style={messageHeaderStyle}>
            <span style={messageRoleStyle}>{message.role === "user" ? "You" : "Agent"}</span>
            {message.message_type !== "analysis" ? (
              <span style={messageTypeBadgeStyle}>{message.message_type.replaceAll("_", " ")}</span>
            ) : null}
            {typeof message.model_metadata?.tool === "string" ? (
              <span style={traceBadgeStyle}>{String(message.model_metadata.tool)}</span>
            ) : null}
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
          {typeof message.model_metadata?.trace_id === "string" ? (
            <p style={messageMetaStyle}>Trace {String(message.model_metadata.trace_id)}</p>
          ) : null}
        </article>
      ))}
      {isLoading ? (
        <div style={assistantMessageStyle}>
          <p style={{ color: "#94A4BD", margin: 0 }}>Refreshing agent context…</p>
        </div>
      ) : null}
    </div>
  );
}

type AgentWorkspacePanelProps = {
  closeRunId: string | undefined;
  entityId: string;
  onRefresh: () => void;
  workspace: ChatThreadWorkspace | null;
};

function AgentWorkspacePanel({
  closeRunId,
  entityId,
  onRefresh,
  workspace,
}: Readonly<AgentWorkspacePanelProps>) {
  const [traceFilter, setTraceFilter] = useState<"all" | "applied" | "pending" | "issues">("all");
  const toolsByIntent = useMemo(() => {
    if (workspace === null) {
      return [];
    }
    const grouped = new Map<string, AgentToolManifestItem[]>();
    for (const tool of workspace.tools) {
      const bucket = grouped.get(tool.intent) ?? [];
      bucket.push(tool);
      grouped.set(tool.intent, bucket);
    }
    return Array.from(grouped.entries());
  }, [workspace]);
  const traceSummary = useMemo(() => buildTraceSummary(workspace?.recent_traces ?? []), [workspace]);
  const visibleTraces = useMemo(() => {
    if (workspace === null) {
      return [];
    }
    if (traceFilter === "all") {
      return workspace.recent_traces;
    }
    if (traceFilter === "issues") {
      return workspace.recent_traces.filter((trace) => isIssueTrace(trace.action_status));
    }
    return workspace.recent_traces.filter((trace) => trace.action_status === traceFilter);
  }, [traceFilter, workspace]);
  const manifestSummary = useMemo(
    () => summarizeManifest(workspace?.mcp_manifest ?? null, workspace?.tools ?? []),
    [workspace],
  );

  return (
    <aside style={workspacePanelStyle}>
      <WorkspaceCard eyebrow="Readiness" title="Run readiness and intake">
        <AgentReadinessPanel
          closeRunId={closeRunId}
          entityId={entityId}
          onRefresh={onRefresh}
          workspace={workspace}
        />
      </WorkspaceCard>

      <WorkspaceCard eyebrow="Memory" title="Working context">
        {workspace === null ? (
          <p style={supportingTextStyle}>Select or create a thread to load agent memory.</p>
        ) : (
          <div style={metricGridStyle}>
            <MetricTile label="Pending approvals" value={String(workspace.memory.pending_action_count)} />
            <MetricTile label="Last tool" value={workspace.memory.last_tool_name ?? "None"} />
            <MetricTile
              label="Last status"
              value={workspace.memory.last_action_status?.replaceAll("_", " ") ?? "Idle"}
            />
            <MetricTile label="Last trace" value={workspace.memory.last_trace_id ?? "Not recorded"} />
          </div>
        )}
        {workspace?.memory.progress_summary ? (
          <p style={supportingTextStyle}>{workspace.memory.progress_summary}</p>
        ) : null}
        {workspace?.memory.updated_at ? (
          <p style={traceMetaStyle}>Last refreshed {formatTimestamp(workspace.memory.updated_at)}</p>
        ) : null}
      </WorkspaceCard>

      <WorkspaceCard eyebrow="Tooling" title="Registered capabilities">
        {workspace === null ? (
          <p style={supportingTextStyle}>Tool metadata appears when the workspace loads.</p>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            <div style={metricGridStyle}>
              <MetricTile label="Tools" value={String(manifestSummary.toolCount)} />
              <MetricTile label="Schema fields" value={String(manifestSummary.schemaFieldCount)} />
              <MetricTile label="Approval-gated" value={String(manifestSummary.approvalCount)} />
              <MetricTile label="Protocol" value={manifestSummary.protocolVersion} />
            </div>
            <p style={supportingTextStyle}>
              These deterministic tools power both the operator workbench and the MCP runtime.
            </p>
            {toolsByIntent.map(([intent, tools]) => (
              <section key={intent} style={{ display: "grid", gap: 8 }}>
                <div style={toolIntentHeaderStyle}>{intent.replaceAll("_", " ")}</div>
                {tools.map((tool) => (
                  <div key={tool.name} style={toolCardStyle}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                      <strong style={toolNameStyle}>{tool.name}</strong>
                      <span style={toolPolicyPillStyle(tool.requires_human_approval)}>
                        {tool.requires_human_approval ? "Approval" : "Auto"}
                      </span>
                    </div>
                    <p style={toolDescriptionStyle}>{tool.description}</p>
                    <code style={toolSignatureStyle}>{tool.prompt_signature}</code>
                    <ToolSchemaPreview tool={tool} />
                  </div>
                ))}
              </section>
            ))}
          </div>
        )}
      </WorkspaceCard>

      <WorkspaceCard eyebrow="Trace" title="Execution timeline">
        {workspace === null || workspace.recent_traces.length === 0 ? (
          <p style={supportingTextStyle}>Trace history appears after the agent responds or applies an action.</p>
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            <div style={metricGridStyle}>
              <MetricTile label="Applied" value={String(traceSummary.appliedCount)} />
              <MetricTile label="Pending" value={String(traceSummary.pendingCount)} />
              <MetricTile label="Issues" value={String(traceSummary.issueCount)} />
              <MetricTile label="Events" value={String(traceSummary.eventCount)} />
            </div>
            <div style={traceFilterRowStyle}>
              {TRACE_FILTER_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  onClick={() => setTraceFilter(option.value)}
                  style={traceFilterPillStyle(traceFilter === option.value)}
                  type="button"
                >
                  {option.label}
                </button>
              ))}
            </div>
            <div style={{ display: "grid", gap: 8 }}>
              {visibleTraces.map((trace) => (
                <TraceItem key={trace.message_id} trace={trace} />
              ))}
            </div>
            {visibleTraces.length === 0 ? (
              <p style={supportingTextStyle}>
                No trace events match the current filter. Switch filters to inspect other execution states.
              </p>
            ) : null}
          </div>
        )}
      </WorkspaceCard>
    </aside>
  );
}

function ToolSchemaPreview({ tool }: Readonly<{ tool: AgentToolManifestItem }>) {
  const schemaFields = useMemo(() => extractToolSchemaFields(tool.input_schema), [tool.input_schema]);
  const schemaSummary = useMemo(() => summarizeToolSchema(tool.input_schema), [tool.input_schema]);

  return (
    <div style={{ display: "grid", gap: 8 }}>
      <div style={schemaSummaryRowStyle}>
        <span style={schemaSummaryPillStyle}>
          {schemaSummary.fieldCount === 0
            ? "No inputs"
            : `${schemaSummary.fieldCount} fields · ${schemaSummary.requiredCount} required`}
        </span>
        {schemaFields.length > 0 ? (
          <div style={schemaFieldChipRowStyle}>
            {schemaFields.slice(0, 3).map((field) => (
              <span key={`${tool.name}-${field.name}`} style={schemaFieldChipStyle(field.required)}>
                {field.name}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {schemaFields.length > 0 ? (
        <details style={toolSchemaDetailsStyle}>
          <summary style={toolSchemaSummaryStyle}>View input contract</summary>
          <div style={toolSchemaFieldListStyle}>
            {schemaFields.map((field) => (
              <ToolSchemaFieldRow key={`${tool.name}-${field.name}`} field={field} />
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

function ToolSchemaFieldRow({ field }: Readonly<{ field: ToolSchemaField }>) {
  return (
    <article style={toolSchemaFieldCardStyle}>
      <div style={{ display: "grid", gap: 4 }}>
        <div style={toolSchemaFieldHeaderStyle}>
          <strong style={toolNameStyle}>{field.name}</strong>
          <div style={toolFieldMetaRowStyle}>
            <span style={toolFieldTypePillStyle}>{field.typeLabel}</span>
            {field.required ? <span style={toolFieldRequiredPillStyle}>Required</span> : null}
            {field.format ? <span style={toolFieldTypePillStyle}>{field.format}</span> : null}
          </div>
        </div>
        <p style={toolDescriptionStyle}>{field.description ?? "No description provided."}</p>
        {field.enumValues.length > 0 ? (
          <div style={schemaFieldChipRowStyle}>
            {field.enumValues.map((value) => (
              <span key={`${field.name}-${value}`} style={toolFieldEnumPillStyle}>
                {value}
              </span>
            ))}
          </div>
        ) : null}
      </div>
    </article>
  );
}

type WorkspaceCardProps = {
  children: ReactNode;
  eyebrow: string;
  title: string;
};

function WorkspaceCard({ children, eyebrow, title }: Readonly<WorkspaceCardProps>) {
  return (
    <section style={workspaceCardStyle}>
      <div style={{ display: "grid", gap: 4 }}>
        <p style={panelEyebrowStyle}>{eyebrow}</p>
        <h3 style={workspaceCardTitleStyle}>{title}</h3>
      </div>
      {children}
    </section>
  );
}

function MetricTile({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <div style={metricTileStyle}>
      <span style={metricLabelStyle}>{label}</span>
      <strong style={metricValueStyle}>{value}</strong>
    </div>
  );
}

function TraceItem({ trace }: Readonly<{ trace: AgentTraceRecord }>) {
  return (
    <div style={traceItemStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
        <div style={{ display: "grid", gap: 4 }}>
          <strong style={toolNameStyle}>{trace.tool_name ?? trace.mode ?? "agent event"}</strong>
          <div style={traceBadgeRowStyle}>
            {trace.mode ? <span style={toolFieldTypePillStyle}>{formatTraceMode(trace.mode)}</span> : null}
            {trace.trace_id ? <span style={toolFieldTypePillStyle}>trace {trace.trace_id}</span> : null}
          </div>
        </div>
        <span style={traceStatusPillStyleForValue(trace.action_status)}>
          {formatTraceStatus(trace.action_status)}
        </span>
      </div>
      {trace.summary ? <p style={toolDescriptionStyle}>{trace.summary}</p> : null}
      <p style={traceMetaStyle}>{formatTimestamp(trace.created_at)}</p>
    </div>
  );
}

function StatusBanner({
  children,
  tone,
}: Readonly<{ children: React.ReactNode; tone: "danger" | "info" }>) {
  return (
    <div style={tone === "danger" ? statusDangerStyle : statusInfoStyle} role="status">
      {children}
    </div>
  );
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return value;
  }
  return date.toLocaleString();
}

function extractInlineAttachments(message: ChatMessageRecord): Array<{
  filename: string;
  intentLabel: string;
}> {
  const rawAttachments = message.grounding_payload.attachments;
  if (!Array.isArray(rawAttachments)) {
    return [];
  }
  const attachmentIntent =
    typeof message.grounding_payload.attachment_intent === "string"
      ? message.grounding_payload.attachment_intent.replaceAll("_", " ")
      : "attachment";
  return rawAttachments
    .filter((item): item is Record<string, unknown> => typeof item === "object" && item !== null)
    .map((item) => ({
      filename: typeof item.filename === "string" ? item.filename : "attached file",
      intentLabel:
        typeof item.intent === "string"
          ? item.intent.replaceAll("_", " ")
          : attachmentIntent,
    }));
}

function toolPolicyPillStyle(requiresApproval: boolean): CSSProperties {
  return {
    background: "rgba(170, 183, 202, 0.1)",
    borderRadius: 999,
    color: requiresApproval ? "#D8B36A" : "#8FDBC5",
    fontSize: 11,
    fontWeight: 700,
    padding: "4px 10px",
  };
}

const TRACE_FILTER_OPTIONS: ReadonlyArray<{
  label: string;
  value: "all" | "applied" | "pending" | "issues";
}> = [
  { label: "All", value: "all" },
  { label: "Applied", value: "applied" },
  { label: "Pending", value: "pending" },
  { label: "Issues", value: "issues" },
];

function buildTraceSummary(traces: readonly AgentTraceRecord[]) {
  return traces.reduce(
    (summary, trace) => ({
      appliedCount: summary.appliedCount + (trace.action_status === "applied" ? 1 : 0),
      eventCount: summary.eventCount + 1,
      issueCount: summary.issueCount + (isIssueTrace(trace.action_status) ? 1 : 0),
      pendingCount: summary.pendingCount + (trace.action_status === "pending" ? 1 : 0),
    }),
    {
      appliedCount: 0,
      eventCount: 0,
      issueCount: 0,
      pendingCount: 0,
    },
  );
}

function summarizeManifest(
  manifest: ChatToolManifest | null,
  tools: readonly AgentToolManifestItem[],
) {
  if (manifest === null) {
    return {
      approvalCount: 0,
      protocolVersion: "unknown",
      schemaFieldCount: 0,
      toolCount: 0,
    };
  }

  return {
    approvalCount: tools.filter((tool) => tool.requires_human_approval).length,
    protocolVersion: manifest.version,
    schemaFieldCount: manifest.tools.reduce(
      (fieldCount, tool) => fieldCount + summarizeToolSchema(tool.inputSchema).fieldCount,
      0,
    ),
    toolCount: manifest.tools.length,
  };
}

function isIssueTrace(actionStatus: string | null): boolean {
  return actionStatus === "rejected" || actionStatus === "failed";
}

function formatTraceMode(mode: string): string {
  return mode.replaceAll("_", " ");
}

function formatTraceStatus(actionStatus: string | null): string {
  if (actionStatus === null) {
    return "recorded";
  }
  return actionStatus.replaceAll("_", " ");
}

function traceStatusPillStyleForValue(actionStatus: string | null): CSSProperties {
  if (actionStatus === "applied") {
    return {
      ...traceStatusPillStyle,
      color: "#8FDBC5",
    };
  }
  if (actionStatus === "pending") {
    return {
      ...traceStatusPillStyle,
      color: "#D8B36A",
    };
  }
  if (isIssueTrace(actionStatus)) {
    return {
      ...traceStatusPillStyle,
      color: "#F28B82",
    };
  }
  return traceStatusPillStyle;
}

function traceFilterPillStyle(isActive: boolean): CSSProperties {
  return {
    background: isActive ? "rgba(79, 142, 247, 0.14)" : "rgba(170, 183, 202, 0.08)",
    border: "1px solid rgba(79, 142, 247, 0.18)",
    borderRadius: 999,
    color: isActive ? "#7EBCFF" : "#AAB7CA",
    cursor: "pointer",
    fontSize: 11,
    fontWeight: 700,
    padding: "6px 10px",
  };
}

const workbenchShellStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "280px minmax(0, 1fr) 360px",
  height: "100%",
  minHeight: 0,
};

const threadSidebarStyle: CSSProperties = {
  background: "#121926",
  borderRight: "1px solid #24324A",
  display: "flex",
  flexDirection: "column",
  gap: 16,
  minHeight: 0,
  padding: 20,
};

const threadSidebarHeaderStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 12,
};

const conversationPaneStyle: CSSProperties = {
  background: "#0F1624",
  display: "flex",
  flexDirection: "column",
  minHeight: 0,
};

const workspacePanelStyle: CSSProperties = {
  background: "#121926",
  borderLeft: "1px solid #24324A",
  display: "flex",
  flexDirection: "column",
  gap: 16,
  minHeight: 0,
  overflow: "auto",
  padding: 20,
};

const workspaceCardStyle: CSSProperties = {
  background: "#172133",
  border: "1px solid #24324A",
  borderRadius: 16,
  display: "grid",
  gap: 14,
  padding: 16,
};

const railContainerStyle: CSSProperties = {
  background: "#121926",
  borderLeft: "1px solid #24324A",
  display: "flex",
  flexDirection: "column",
  height: "100%",
  width: 420,
};

const railHeaderStyle: CSSProperties = {
  alignItems: "center",
  borderBottom: "1px solid #24324A",
  display: "flex",
  justifyContent: "space-between",
  padding: "14px 16px",
};

const railTitleStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 16,
  fontWeight: 700,
  margin: 0,
};

const railSubtitleStyle: CSSProperties = {
  color: "#94A4BD",
  fontSize: 12,
  margin: 0,
};

const conversationHeaderStyle: CSSProperties = {
  borderBottom: "1px solid #24324A",
  display: "grid",
  gap: 12,
  padding: "20px 24px 16px",
};

const conversationBodyStyle: CSSProperties = {
  color: "#AAB7CA",
  fontSize: 14,
  lineHeight: "22px",
  margin: 0,
  maxWidth: 760,
};

const conversationBadgeRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
};

const threadListStyle: CSSProperties = {
  display: "grid",
  gap: 8,
  listStyle: "none",
  margin: 0,
  overflow: "auto",
  padding: 0,
};

const threadItemStyle: CSSProperties = {
  background: "#172133",
  border: "1px solid #24324A",
  borderRadius: 14,
  color: "#F2F5FA",
  cursor: "pointer",
  display: "block",
  padding: "14px 16px",
  textAlign: "left",
  width: "100%",
};

const threadItemActiveStyle: CSSProperties = {
  borderColor: "#4F8EF7",
  boxShadow: "0 0 0 1px rgba(79, 142, 247, 0.18)",
};

const threadTitleStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 14,
  fontWeight: 600,
};

const threadMetaStyle: CSSProperties = {
  color: "#94A4BD",
  fontSize: 12,
};

const messageListStyle: CSSProperties = {
  display: "flex",
  flex: 1,
  flexDirection: "column",
  gap: 12,
  minHeight: 0,
  overflow: "auto",
  padding: "20px 24px",
};

const assistantMessageStyle: CSSProperties = {
  alignSelf: "stretch",
  background: "#172133",
  border: "1px solid #24324A",
  borderRadius: 16,
  display: "grid",
  gap: 8,
  maxWidth: "88%",
  padding: "14px 16px",
};

const userMessageStyle: CSSProperties = {
  ...assistantMessageStyle,
  alignSelf: "flex-end",
  background: "#1D2B43",
  borderColor: "#36517D",
};

const messageHeaderStyle: CSSProperties = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
};

const messageRoleStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 12,
  fontWeight: 700,
};

const messageTypeBadgeStyle: CSSProperties = {
  background: "rgba(79, 142, 247, 0.12)",
  borderRadius: 999,
  color: "#7EBCFF",
  fontSize: 11,
  padding: "2px 8px",
};

const traceBadgeStyle: CSSProperties = {
  background: "rgba(216, 179, 106, 0.12)",
  borderRadius: 999,
  color: "#D8B36A",
  fontSize: 11,
  padding: "2px 8px",
};

const messageContentStyle: CSSProperties = {
  color: "#E4EBF5",
  fontSize: 14,
  lineHeight: "22px",
  margin: 0,
  whiteSpace: "pre-wrap",
};

const messageMetaStyle: CSSProperties = {
  color: "#7B8AA3",
  fontSize: 11,
  margin: 0,
};

const panelEyebrowStyle: CSSProperties = {
  color: "#7EBCFF",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
};

const panelTitleStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 20,
  fontWeight: 700,
  margin: 0,
};

const workspaceCardTitleStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 16,
  fontWeight: 700,
  margin: 0,
};

const supportingTextStyle: CSSProperties = {
  color: "#AAB7CA",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
};

const metricGridStyle: CSSProperties = {
  display: "grid",
  gap: 10,
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
};

const metricTileStyle: CSSProperties = {
  background: "#101826",
  border: "1px solid #24324A",
  borderRadius: 12,
  display: "grid",
  gap: 6,
  padding: "12px 12px 10px",
};

const metricLabelStyle: CSSProperties = {
  color: "#94A4BD",
  fontSize: 11,
  textTransform: "uppercase",
};

const metricValueStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 14,
};

const toolIntentHeaderStyle: CSSProperties = {
  color: "#94A4BD",
  fontSize: 11,
  fontWeight: 700,
  textTransform: "uppercase",
};

const toolCardStyle: CSSProperties = {
  background: "#101826",
  border: "1px solid #24324A",
  borderRadius: 12,
  display: "grid",
  gap: 8,
  padding: 12,
};

const toolNameStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 13,
};

const toolDescriptionStyle: CSSProperties = {
  color: "#AAB7CA",
  fontSize: 12,
  lineHeight: "18px",
  margin: 0,
};

const toolSignatureStyle: CSSProperties = {
  color: "#7EBCFF",
  fontSize: 11,
  overflowWrap: "anywhere",
};

const schemaSummaryRowStyle: CSSProperties = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  justifyContent: "space-between",
};

const schemaSummaryPillStyle: CSSProperties = {
  background: "rgba(79, 142, 247, 0.12)",
  borderRadius: 999,
  color: "#7EBCFF",
  fontSize: 11,
  fontWeight: 700,
  padding: "4px 10px",
};

const schemaFieldChipRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 6,
};

function schemaFieldChipStyle(isRequired: boolean): CSSProperties {
  return {
    background: isRequired ? "rgba(216, 179, 106, 0.12)" : "rgba(170, 183, 202, 0.1)",
    borderRadius: 999,
    color: isRequired ? "#D8B36A" : "#AAB7CA",
    fontSize: 10,
    fontWeight: 700,
    padding: "4px 8px",
  };
}

const toolSchemaDetailsStyle: CSSProperties = {
  background: "rgba(12, 18, 30, 0.65)",
  border: "1px solid rgba(36, 50, 74, 0.9)",
  borderRadius: 10,
  padding: "8px 10px",
};

const toolSchemaSummaryStyle: CSSProperties = {
  color: "#F2F5FA",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 700,
};

const toolSchemaFieldListStyle: CSSProperties = {
  display: "grid",
  gap: 8,
  marginTop: 10,
};

const toolSchemaFieldCardStyle: CSSProperties = {
  background: "#0D1522",
  border: "1px solid #223148",
  borderRadius: 10,
  padding: 10,
};

const toolSchemaFieldHeaderStyle: CSSProperties = {
  alignItems: "center",
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
  justifyContent: "space-between",
};

const toolFieldMetaRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 6,
};

const toolFieldTypePillStyle: CSSProperties = {
  background: "rgba(170, 183, 202, 0.1)",
  borderRadius: 999,
  color: "#AAB7CA",
  fontSize: 10,
  fontWeight: 700,
  padding: "3px 8px",
};

const toolFieldRequiredPillStyle: CSSProperties = {
  background: "rgba(216, 179, 106, 0.12)",
  borderRadius: 999,
  color: "#D8B36A",
  fontSize: 10,
  fontWeight: 700,
  padding: "3px 8px",
};

const toolFieldEnumPillStyle: CSSProperties = {
  background: "rgba(143, 219, 197, 0.12)",
  borderRadius: 999,
  color: "#8FDBC5",
  fontSize: 10,
  fontWeight: 700,
  padding: "3px 8px",
};

const traceItemStyle: CSSProperties = {
  background: "#101826",
  border: "1px solid #24324A",
  borderRadius: 12,
  display: "grid",
  gap: 6,
  padding: 12,
};

const traceMetaStyle: CSSProperties = {
  color: "#7B8AA3",
  fontSize: 11,
  margin: 0,
};

const traceBadgeRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 6,
};

const traceFilterRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 8,
};

const inlineAttachmentRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 6,
  marginBottom: 8,
};

const inlineAttachmentPillStyle: CSSProperties = {
  background: "rgba(76, 139, 245, 0.12)",
  border: "1px solid rgba(76, 139, 245, 0.28)",
  borderRadius: 999,
  color: "#A8CBFF",
  fontSize: 11,
  padding: "4px 8px",
  textTransform: "capitalize",
};

const traceStatusPillStyle: CSSProperties = {
  background: "rgba(170, 183, 202, 0.1)",
  borderRadius: 999,
  color: "#AAB7CA",
  fontSize: 11,
  fontWeight: 700,
  padding: "4px 10px",
};

const emptyStateCardStyle: CSSProperties = {
  background: "#172133",
  border: "1px dashed #30415F",
  borderRadius: 16,
  display: "grid",
  gap: 8,
  padding: 18,
};

const emptyTitleStyle: CSSProperties = {
  color: "#F2F5FA",
  fontSize: 14,
  fontWeight: 700,
  margin: 0,
};

const emptyBodyStyle: CSSProperties = {
  color: "#AAB7CA",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
};

const primaryButtonStyle: CSSProperties = {
  background: "#4F8EF7",
  border: "none",
  borderRadius: 10,
  color: "#F2F5FA",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 700,
  padding: "10px 14px",
};

const ghostButtonStyle: CSSProperties = {
  background: "transparent",
  border: "1px solid #30415F",
  borderRadius: 10,
  color: "#AAB7CA",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 700,
  padding: "8px 10px",
};

const groundingBadgeStyle: CSSProperties = {
  background: "rgba(79, 142, 247, 0.12)",
  borderRadius: 999,
  color: "#7EBCFF",
  fontSize: 11,
  fontWeight: 700,
  padding: "4px 10px",
};

const metaPillStyle: CSSProperties = {
  background: "rgba(170, 183, 202, 0.1)",
  borderRadius: 999,
  color: "#AAB7CA",
  fontSize: 11,
  fontWeight: 700,
  padding: "4px 10px",
};

const statusDangerStyle: CSSProperties = {
  background: "rgba(217, 83, 79, 0.12)",
  border: "1px solid rgba(217, 83, 79, 0.28)",
  borderRadius: 12,
  color: "#F2B5B2",
  fontSize: 12,
  padding: "10px 12px",
};

const statusInfoStyle: CSSProperties = {
  background: "rgba(79, 142, 247, 0.12)",
  border: "1px solid rgba(79, 142, 247, 0.28)",
  borderRadius: 12,
  color: "#D8E6FF",
  fontSize: 12,
  padding: "10px 12px",
};

const expandButtonStyle: CSSProperties = {
  background: "#4F8EF7",
  border: "none",
  borderRadius: 12,
  bottom: 16,
  color: "#F2F5FA",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 700,
  padding: "10px 16px",
  position: "fixed",
  right: 16,
  zIndex: 50,
};
