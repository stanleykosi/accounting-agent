/*
Purpose: Centralize same-origin chat data access for the desktop UI.
Scope: Chat thread creation, listing, detail reads, and message sends grounded
to entity and close run context.
Dependencies: Browser Fetch APIs, existing `/api/**` proxy routes, and strict
runtime response guards.
*/

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
const BACKEND_CHAT_BASE = buildBackendChatUrl("");

/**
 * Purpose: Build the canonical backend chat URL targeted by server-side proxy routes.
 * Inputs: A route suffix under the FastAPI `/chat` router.
 * Outputs: A fully qualified backend chat URL.
 * Behavior: Uses one canonical backend base URL and strips duplicate slashes.
 */
export function buildBackendChatUrl(path: string): string {
  const normalizedBaseUrl = (process.env.ACCOUNTING_AGENT_API_URL ?? "http://127.0.0.1:8000/api")
    .replace(/\/+$/u, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBaseUrl}/chat${normalizedPath}`;
}

/**
 * Purpose: Execute an authenticated fetch and return parsed JSON or raise a typed error.
 * Inputs: Request URL, optional init overrides, and caller auth context.
 * Outputs: Parsed JSON response body.
 * Behavior: Throws `ChatApiError` on non-2xx responses so callers can surface recovery steps.
 */
async function fetchJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
    },
    ...init,
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ChatApiError(
      response.status,
      (body as Record<string, unknown>)?.message as string | undefined,
      (body as Record<string, unknown>)?.code as string | undefined,
    );
  }

  return (await response.json()) as T;
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

  return fetchJson<ChatThreadWithMessages>(
    `${API_BASE}/threads/${threadId}?${params.toString()}`,
  );
}

/**
 * Purpose: Send a user message and receive a grounded read-only copilot analysis response.
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
