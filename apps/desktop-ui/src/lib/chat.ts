/*
Purpose: Centralize same-origin chat data access for the desktop UI.
Scope: Chat thread creation, listing, detail reads, and message sends grounded
to entity and close run context.
Dependencies: Browser Fetch APIs, existing `/api/**` proxy routes, and strict
runtime response guards.
*/

import { resolveBackendApiBaseUrl } from "./runtime";

export type ChatMessageRole = "user" | "assistant" | "system";
export type ChatMessageType = "analysis" | "workflow" | "action" | "warning";

export type GroundingContext = {
  autonomy_mode: string;
  base_currency: string;
  close_run_id: string | null;
  entity_id: string;
  entity_name: string;
  period_label: string | null;
};

export type ChatThreadSummary = {
  close_run_id: string | null;
  created_at: string;
  entity_id: string;
  grounding: GroundingContext;
  id: string;
  last_message_at: string | null;
  message_count: number;
  title: string | null;
  updated_at: string;
};

export type ChatMessageRecord = {
  content: string;
  created_at: string;
  grounding_payload: Record<string, unknown>;
  id: string;
  linked_action_id: string | null;
  message_type: ChatMessageType;
  model_metadata: Record<string, unknown> | null;
  role: ChatMessageRole;
  thread_id: string;
};

export type ChatThreadWithMessages = {
  messages: ChatMessageRecord[];
  thread: ChatThreadSummary;
};

export type ChatThreadDeleteResponse = {
  deleted_message_count: number;
  deleted_thread_id: string;
  deleted_thread_title: string | null;
};

export type ChatAttachmentIntent = "source_documents";

export type AgentMemorySummary = {
  last_action_status: string | null;
  last_assistant_response: string | null;
  last_operator_message: string | null;
  last_tool_name: string | null;
  last_trace_id: string | null;
  pending_action_count: number;
  progress_summary: string | null;
  recent_tool_names: string[];
  updated_at: string | null;
};

export type AgentCoaAccountSummary = {
  account_code: string;
  account_name: string;
  account_type: string;
  is_active: boolean;
  is_postable: boolean;
};

export type AgentCoaSummary = {
  account_count: number;
  accounts: AgentCoaAccountSummary[];
  activated_at: string | null;
  is_available: boolean;
  postable_account_count: number;
  requires_operator_upload: boolean;
  source: string | null;
  status: string;
  summary: string | null;
  version_no: number | null;
};

export type AgentRunPhaseState = {
  blocking_reason: string | null;
  completed_at: string | null;
  label: string;
  phase: string;
  status: string;
};

export type AgentRunReadiness = {
  blockers: string[];
  document_count: number;
  has_close_run: boolean;
  has_source_documents: boolean;
  next_actions: string[];
  parsed_document_count: number;
  phase_states: AgentRunPhaseState[];
  status: string;
  warnings: string[];
};

export type AgentToolManifestItem = {
  description: string;
  input_schema: Record<string, unknown>;
  intent: string;
  name: string;
  prompt_signature: string;
  requires_human_approval: boolean;
};

export type AgentTraceRecord = {
  action_status: string | null;
  created_at: string;
  message_id: string;
  mode: string | null;
  summary: string | null;
  tool_name: string | null;
  trace_id: string | null;
};

export type ToolSchemaNode = {
  additionalProperties?: boolean | ToolSchemaNode;
  description?: string;
  enum?: unknown[];
  format?: string;
  items?: ToolSchemaNode;
  properties?: Record<string, ToolSchemaNode>;
  required?: string[];
  type?: string | string[];
};

export type ChatToolManifestTool = {
  description: string;
  inputSchema: ToolSchemaNode;
  name: string;
};

export type ChatToolManifest = {
  protocol: string;
  tools: ChatToolManifestTool[];
  version: string;
};

export type ToolSchemaField = {
  description: string | null;
  enumValues: string[];
  format: string | null;
  name: string;
  required: boolean;
  typeLabel: string;
};

export type ChatThreadWorkspace = {
  coa: AgentCoaSummary;
  grounding: GroundingContext;
  mcp_manifest: ChatToolManifest;
  memory: AgentMemorySummary;
  progress_summary: string | null;
  readiness: AgentRunReadiness;
  recent_traces: AgentTraceRecord[];
  thread_id: string;
  tools: AgentToolManifestItem[];
};

export type ChatThreadListResponse = {
  threads: ChatThreadSummary[];
};

export type ChatMessageResponse = {
  message: ChatMessageRecord;
  user_message: ChatMessageRecord | null;
};

export type CreateChatThreadRequest = {
  close_run_id?: string;
  entity_id: string;
  title?: string;
};

export type SendChatMessageRequest = {
  content: string;
};

const API_BASE = "/api/chat";

/**
 * Purpose: Build the canonical backend chat URL targeted by server-side proxy routes.
 * Inputs: A route suffix under the FastAPI `/chat` router.
 * Outputs: A fully qualified backend chat URL.
 * Behavior: Uses one canonical backend base URL and strips duplicate slashes.
 */
export function buildBackendChatUrl(path: string): string {
  const normalizedBaseUrl = resolveBackendApiBaseUrl();
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBaseUrl}/chat${normalizedPath}`;
}

/**
 * Purpose: Execute an authenticated fetch and return parsed JSON or raise a typed error.
 * Inputs: Request URL, optional init overrides, and caller auth context.
 * Outputs: Parsed JSON response body.
 * Behavior: Throws `ChatApiError` on non-2xx responses so callers can surface recovery steps.
 */
async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
    },
    ...init,
  });

  if (!response.ok) {
    const body = await parseJsonResponse(response);
    throw new ChatApiError(
      response.status,
      extractChatApiErrorMessage(body),
      extractChatApiErrorCode(body),
    );
  }

  return (await parseJsonResponse(response)) as T;
}

/**
 * Purpose: Represent a typed API failure so UI surfaces can render recovery actions.
 * Scope: HTTP status, optional stable error code, and operator-facing message.
 */
export class ChatApiError extends Error {
  code: string | undefined;
  status: number;

  constructor(status: number, message?: string, code?: string) {
    super(message ?? `Chat API request failed with status ${status}.`);
    this.name = "ChatApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * Purpose: Create a new grounded chat thread scoped to an entity and optional close run.
 * Inputs: Thread creation payload with entity and optional close run identifiers.
 * Outputs: The created thread with an empty message list.
 * Behavior: Posts to `/api/chat/threads` and throws `ChatApiError` on failure.
 */
export async function createChatThread(
  request: CreateChatThreadRequest,
): Promise<ChatThreadWithMessages> {
  return fetchJson<ChatThreadWithMessages>(`${API_BASE}/threads`, {
    body: JSON.stringify(request),
    method: "POST",
  });
}

/**
 * Purpose: List chat threads for an entity or close run, ordered newest-first.
 * Inputs: Entity ID, optional close run ID, and pagination limit.
 * Outputs: Thread list response with summaries and message counts.
 * Behavior: Builds query parameters and fetches from the chat API.
 */
export async function listChatThreads(
  entityId: string,
  options?: { closeRunId?: string; limit?: number },
): Promise<ChatThreadListResponse> {
  const params = new URLSearchParams();
  params.set("entity_id", entityId);
  if (options?.closeRunId) {
    params.set("close_run_id", options.closeRunId);
  }
  if (options?.limit) {
    params.set("limit", String(options.limit));
  }

  return fetchJson<ChatThreadListResponse>(`${API_BASE}/threads?${params.toString()}`);
}

/**
 * Purpose: Read one chat thread with its message history for detail views.
 * Inputs: Thread ID, entity ID for access verification, and message limit.
 * Outputs: Thread summary with chronologically ordered messages.
 * Behavior: Fetches from the thread detail endpoint.
 */
export async function getChatThread(
  threadId: string,
  entityId: string,
  messageLimit?: number,
): Promise<ChatThreadWithMessages> {
  const params = new URLSearchParams();
  params.set("entity_id", entityId);
  if (messageLimit) {
    params.set("message_limit", String(messageLimit));
  }

  return fetchJson<ChatThreadWithMessages>(`${API_BASE}/threads/${threadId}?${params.toString()}`);
}

export async function deleteChatThread(
  threadId: string,
  entityId: string,
): Promise<ChatThreadDeleteResponse> {
  return fetchJson<ChatThreadDeleteResponse>(
    `${API_BASE}/threads/${threadId}?entity_id=${encodeURIComponent(entityId)}`,
    {
      method: "DELETE",
    },
  );
}

export async function getChatThreadWorkspace(
  threadId: string,
  entityId: string,
): Promise<ChatThreadWorkspace> {
  const workspace = await fetchJson<ChatThreadWorkspace>(
    `${API_BASE}/threads/${threadId}/workspace?entity_id=${encodeURIComponent(entityId)}`,
  );
  return {
    ...workspace,
    mcp_manifest: normalizeChatToolManifest(workspace.mcp_manifest as Record<string, unknown>),
  };
}

/**
 * Purpose: Send a user message and receive a grounded read-only agent analysis response.
 * Inputs: Thread ID, entity ID for access verification, and message content.
 * Outputs: The persisted user message and the generated assistant response with evidence.
 * Behavior: Posts to the thread messages endpoint.
 */
export async function sendChatMessage(
  threadId: string,
  entityId: string,
  content: string,
): Promise<ChatMessageResponse> {
  return fetchJson<ChatMessageResponse>(
    `${API_BASE}/threads/${threadId}/messages?entity_id=${encodeURIComponent(entityId)}`,
    {
      body: JSON.stringify({ content }),
      method: "POST",
    },
  );
}

// ---------------------------------------------------------------------------
// Chat action routing types and helpers (Step 35)
// ---------------------------------------------------------------------------

export type ChatActionIntent =
  | "proposed_edit"
  | "approval_request"
  | "document_request"
  | "explanation"
  | "workflow_action"
  | "reconciliation_query"
  | "report_action";

export type ChatActionSummary = {
  created_at: string;
  id: string;
  intent: ChatActionIntent;
  requires_human_approval: boolean;
  status: "pending" | "approved" | "rejected" | "superseded" | "applied";
  target_id: string | null;
  target_type: string | null;
  thread_id: string;
};

export type SendChatActionRequest = {
  content: string;
  force_action_mode?: boolean;
};

export type ChatActionResponse = {
  action_plan: ChatActionSummary | null;
  content: string;
  is_read_only: boolean;
  message_id: string;
};

export type ApproveChatActionRequest = {
  reason?: string;
};

export type RejectChatActionRequest = {
  reason: string;
};

/**
 * Purpose: Send a message with action intent detection and routing.
 * Inputs: Thread ID, entity ID, and message content.
 * Outputs: Assistant response with optional action execution plan.
 * Behavior: Posts to the action endpoint; returns read-only when no action detected.
 */
export async function sendChatAction(
  threadId: string,
  entityId: string,
  content: string,
): Promise<ChatActionResponse> {
  return fetchJson<ChatActionResponse>(
    `${API_BASE}/threads/${threadId}/actions?entity_id=${encodeURIComponent(entityId)}`,
    {
      body: JSON.stringify({ content } satisfies SendChatActionRequest),
      method: "POST",
    },
  );
}

export async function sendChatActionWithAttachments(
  threadId: string,
  entityId: string,
  input: {
    attachmentIntent: ChatAttachmentIntent;
    content?: string;
    files: readonly File[];
  },
): Promise<ChatActionResponse> {
  const formData = new FormData();
  formData.append("attachment_intent", input.attachmentIntent);
  if (typeof input.content === "string") {
    formData.append("content", input.content);
  }
  for (const file of input.files) {
    formData.append("files", file);
  }

  const response = await fetch(
    `${API_BASE}/threads/${threadId}/actions/attachments?entity_id=${encodeURIComponent(entityId)}`,
    {
      body: formData,
      credentials: "include",
      method: "POST",
    },
  );

  if (!response.ok) {
    const body = await parseJsonResponse(response);
    throw new ChatApiError(
      response.status,
      extractChatApiErrorMessage(body),
      extractChatApiErrorCode(body),
    );
  }

  return (await parseJsonResponse(response)) as ChatActionResponse;
}

/**
 * Purpose: List pending action plans for a chat thread.
 * Inputs: Thread ID, entity ID, and optional limit.
 * Outputs: Array of pending action summaries for review rendering.
 */
export async function listThreadActions(
  threadId: string,
  entityId: string,
  limit = 50,
): Promise<ChatActionSummary[]> {
  const params = new URLSearchParams();
  params.set("entity_id", entityId);
  params.set("limit", String(limit));

  return fetchJson<ChatActionSummary[]>(
    `${API_BASE}/threads/${threadId}/actions?${params.toString()}`,
  );
}

/**
 * Purpose: Approve a pending chat-originated action plan.
 * Inputs: Action plan ID, thread ID, entity ID, and optional reason.
 * Outputs: Updated action summary with approved status.
 */
export async function approveChatAction(
  actionPlanId: string,
  threadId: string,
  entityId: string,
  reason?: string,
): Promise<ChatActionSummary> {
  const body: ApproveChatActionRequest = reason !== undefined ? { reason } : {};
  return fetchJson<ChatActionSummary>(
    `${API_BASE}/actions/${encodeURIComponent(actionPlanId)}/approve?thread_id=${encodeURIComponent(threadId)}&entity_id=${encodeURIComponent(entityId)}`,
    {
      body: JSON.stringify(body),
      method: "POST",
    },
  );
}

/**
 * Purpose: Reject a pending chat-originated action plan with a required reason.
 * Inputs: Action plan ID, thread ID, entity ID, and rejection reason.
 * Outputs: Updated action summary with rejected status.
 */
export async function rejectChatAction(
  actionPlanId: string,
  threadId: string,
  entityId: string,
  reason: string,
): Promise<ChatActionSummary> {
  return fetchJson<ChatActionSummary>(
    `${API_BASE}/actions/${encodeURIComponent(actionPlanId)}/reject?thread_id=${encodeURIComponent(threadId)}&entity_id=${encodeURIComponent(entityId)}`,
    {
      body: JSON.stringify({ reason } satisfies RejectChatActionRequest),
      method: "POST",
    },
  );
}

export async function getChatToolManifest(): Promise<ChatToolManifest> {
  const manifest = await fetchJson<Record<string, unknown>>(`${API_BASE}/tools/mcp`);
  return normalizeChatToolManifest(manifest);
}

export function normalizeChatToolManifest(
  manifest: Record<string, unknown> | null | undefined,
): ChatToolManifest {
  const safeManifest = isRecord(manifest) ? manifest : {};
  const tools = Array.isArray(safeManifest.tools) ? safeManifest.tools : [];

  return {
    protocol:
      typeof safeManifest.protocol === "string"
        ? safeManifest.protocol
        : "model-context-protocol",
    tools: tools
      .filter(isRecord)
      .map((tool) => ({
        description: typeof tool.description === "string" ? tool.description : "",
        inputSchema: normalizeToolSchemaNode(tool.inputSchema),
        name: typeof tool.name === "string" ? tool.name : "unknown_tool",
      })),
    version: typeof safeManifest.version === "string" ? safeManifest.version : "unknown",
  };
}

export function summarizeToolSchema(
  schema: Record<string, unknown> | ToolSchemaNode,
): {
  fieldCount: number;
  requiredCount: number;
} {
  const fields = extractToolSchemaFields(schema);
  return {
    fieldCount: fields.length,
    requiredCount: fields.filter((field) => field.required).length,
  };
}

export function extractToolSchemaFields(
  schema: Record<string, unknown> | ToolSchemaNode,
): ToolSchemaField[] {
  const normalizedSchema = normalizeToolSchemaNode(schema);
  const collectedFields = collectToolSchemaFields(normalizedSchema);
  return collectedFields.sort((left, right) => left.name.localeCompare(right.name));
}

function collectToolSchemaFields(
  schema: ToolSchemaNode,
  prefix = "",
  required = true,
): ToolSchemaField[] {
  const properties = schema.properties;
  const requiredFields = new Set(schema.required ?? []);
  if (properties && Object.keys(properties).length > 0) {
    return Object.entries(properties).flatMap(([name, childSchema]) =>
      collectToolSchemaFields(
        childSchema,
        prefix ? `${prefix}.${name}` : name,
        requiredFields.has(name),
      ),
    );
  }

  if (!prefix) {
    return [];
  }

  return [
    {
      description: schema.description ?? null,
      enumValues: Array.isArray(schema.enum)
        ? schema.enum.filter((value): value is string => typeof value === "string")
        : [],
      format: typeof schema.format === "string" ? schema.format : null,
      name: prefix,
      required,
      typeLabel: formatToolSchemaTypeLabel(schema),
    },
  ];
}

function formatToolSchemaTypeLabel(schema: ToolSchemaNode): string {
  const normalizedType = schema.type;
  if (Array.isArray(normalizedType)) {
    return normalizedType.join(" | ");
  }
  if (normalizedType === "array") {
    const itemType = schema.items ? formatToolSchemaTypeLabel(schema.items) : "unknown";
    return `array<${itemType}>`;
  }
  if (typeof normalizedType === "string") {
    return normalizedType;
  }
  if (Array.isArray(schema.enum) && schema.enum.length > 0) {
    return "enum";
  }
  return "value";
}

function normalizeToolSchemaNode(value: unknown): ToolSchemaNode {
  if (!isRecord(value)) {
    return {};
  }

  const normalizedProperties: Record<string, ToolSchemaNode> = {};
  if (isRecord(value.properties)) {
    for (const [propertyName, propertyValue] of Object.entries(value.properties)) {
      normalizedProperties[propertyName] = normalizeToolSchemaNode(propertyValue);
    }
  }

  let normalizedAdditionalProperties: boolean | ToolSchemaNode | undefined;
  if (typeof value.additionalProperties === "boolean") {
    normalizedAdditionalProperties = value.additionalProperties;
  } else if (isRecord(value.additionalProperties)) {
    normalizedAdditionalProperties = normalizeToolSchemaNode(value.additionalProperties);
  }

  const normalizedNode: ToolSchemaNode = {};
  if (normalizedAdditionalProperties !== undefined) {
    normalizedNode.additionalProperties = normalizedAdditionalProperties;
  }
  if (typeof value.description === "string") {
    normalizedNode.description = value.description;
  }
  if (Array.isArray(value.enum)) {
    normalizedNode.enum = value.enum;
  }
  if (typeof value.format === "string") {
    normalizedNode.format = value.format;
  }
  const normalizedItems = normalizeToolSchemaItems(value.items);
  if (normalizedItems !== undefined) {
    normalizedNode.items = normalizedItems;
  }
  if (Object.keys(normalizedProperties).length > 0) {
    normalizedNode.properties = normalizedProperties;
  }
  if (
    Array.isArray(value.required) &&
    value.required.every((item): item is string => typeof item === "string")
  ) {
    normalizedNode.required = value.required;
  }
  if (typeof value.type === "string") {
    normalizedNode.type = value.type;
  } else if (
    Array.isArray(value.type) &&
    value.type.every((item): item is string => typeof item === "string")
  ) {
    normalizedNode.type = value.type;
  }
  return normalizedNode;
}

function normalizeToolSchemaItems(value: unknown): ToolSchemaNode | undefined {
  return isRecord(value) ? normalizeToolSchemaNode(value) : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function extractChatApiErrorMessage(body: unknown): string | undefined {
  const bodyRecord = isRecord(body) ? body : null;
  if (typeof bodyRecord?.message === "string") {
    return bodyRecord.message;
  }

  const detail = isRecord(bodyRecord?.detail) ? bodyRecord.detail : null;
  if (typeof detail?.message === "string") {
    return detail.message;
  }

  return undefined;
}

function extractChatApiErrorCode(body: unknown): string | undefined {
  const bodyRecord = isRecord(body) ? body : null;
  if (typeof bodyRecord?.code === "string") {
    return bodyRecord.code;
  }

  const detail = isRecord(bodyRecord?.detail) ? bodyRecord.detail : null;
  if (typeof detail?.code === "string") {
    return detail.code;
  }

  return undefined;
}

async function parseJsonResponse(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.length === 0) {
    return null;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}
