/*
Purpose: Provide same-origin recommendation and journal API helpers for hosted workflow review pages.
Scope: Recommendation generation, listing, approval/rejection, journal review, and apply actions.
Dependencies: Browser Fetch APIs and existing `/api/entities/**` proxy routes.
*/

import {
  buildEntityCacheInvalidationPrefixes,
  invalidateClientCacheByPrefix,
  loadClientCachedValue,
} from "./client-cache";
import { buildEntityProxyPath } from "./entity-proxy";

export type RecommendationSummary = {
  close_run_id: string;
  confidence: number;
  created_at: string;
  document_id: string | null;
  id: string;
  prompt_version: string;
  reasoning_summary: string;
  recommendation_type: string;
  rule_version: string;
  schema_version: string;
  status: string;
  source_document_filename: string | null;
  source_document_type: string | null;
  updated_at: string;
};

export type JournalLineSummary = {
  account_code: string;
  amount: string;
  description: string | null;
  dimensions: Record<string, string>;
  id: string;
  line_no: number;
  line_type: string;
  reference: string | null;
};

export type JournalPostingTarget = "internal_ledger" | "external_erp_package";

export const JOURNAL_POSTING_TARGET_LABELS: Readonly<Record<JournalPostingTarget, string>> = {
  internal_ledger: "Internal ledger",
  external_erp_package: "ERP import package",
};

export type JournalPostingSummary = {
  artifact_filename: string | null;
  artifact_id: string | null;
  artifact_storage_key: string | null;
  artifact_type: string | null;
  id: string;
  note: string | null;
  posted_at: string;
  posted_by_user_id: string | null;
  posting_metadata: Record<string, unknown>;
  posting_target: JournalPostingTarget;
  provider: string | null;
  status: string;
};

export type JournalSummary = {
  applied_by_user_id: string | null;
  approved_by_user_id: string | null;
  close_run_id: string;
  created_at: string;
  description: string;
  entity_id: string;
  id: string;
  journal_number: string;
  line_count: number;
  lines: readonly JournalLineSummary[];
  postings: readonly JournalPostingSummary[];
  posting_date: string;
  recommendation_id: string | null;
  reasoning_summary: string | null;
  status: string;
  total_credits: string;
  total_debits: string;
  updated_at: string;
};

export type RecommendationGenerationResponse = {
  queued_count: number;
  queued_jobs: readonly {
    document_id: string;
    job_id: string;
    status: string;
    task_name: string;
  }[];
  skipped_documents: readonly {
    document_id: string;
    reason: string;
    status: string;
  }[];
  skipped_document_ids: readonly string[];
};

export class RecommendationApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code: string | null = null,
  ) {
    super(message);
    this.name = "RecommendationApiError";
  }
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const requestMethod = normalizeRequestMethod(init.method);

  const performRequest = async (): Promise<T> => {
    const response = await fetch(url, {
      ...init,
      cache: "no-store",
      credentials: "include",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...init.headers,
      },
    });

    const payload = await parseJsonResponse(response);
    if (!response.ok) {
      const detail = isRecord(payload) ? payload.detail : null;
      throw new RecommendationApiError(
        isRecord(detail) && typeof detail.message === "string"
          ? detail.message
          : `Request failed with status ${response.status}.`,
        response.status,
        isRecord(detail) && typeof detail.code === "string" ? detail.code : null,
      );
    }

    return payload as T;
  };

  if (requestMethod === "GET") {
    return loadClientCachedValue(url, performRequest);
  }

  const payload = await performRequest();
  invalidateClientCacheByPrefix(buildEntityCacheInvalidationPrefixes(url));
  return payload;
}

export async function listRecommendations(
  entityId: string,
  closeRunId: string,
): Promise<readonly RecommendationSummary[]> {
  const payload = await requestJson<{ recommendations: RecommendationSummary[] }>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "recommendations"]),
    { method: "GET" },
  );
  return payload.recommendations;
}

export async function generateRecommendations(
  entityId: string,
  closeRunId: string,
  options?: { force?: boolean },
): Promise<RecommendationGenerationResponse> {
  return requestJson<RecommendationGenerationResponse>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "recommendations", "generate"]),
    {
      method: "POST",
      body: JSON.stringify({ force: options?.force ?? false }),
    },
  );
}

export async function approveRecommendation(
  entityId: string,
  closeRunId: string,
  recommendationId: string,
  reason?: string | null,
): Promise<void> {
  await requestJson(
    buildEntityProxyPath(entityId, [
      "close-runs",
      closeRunId,
      "recommendations",
      recommendationId,
      "approve",
    ]),
    {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
}

export async function rejectRecommendation(
  entityId: string,
  closeRunId: string,
  recommendationId: string,
  reason: string,
): Promise<void> {
  await requestJson(
    buildEntityProxyPath(entityId, [
      "close-runs",
      closeRunId,
      "recommendations",
      recommendationId,
      "reject",
    ]),
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
}

export async function listJournals(
  entityId: string,
  closeRunId: string,
): Promise<readonly JournalSummary[]> {
  const payload = await requestJson<{ journals: JournalSummary[] }>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "journals"]),
    { method: "GET" },
  );
  return payload.journals;
}

export async function readJournal(
  entityId: string,
  closeRunId: string,
  journalId: string,
): Promise<JournalSummary> {
  return requestJson<JournalSummary>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "journals", journalId]),
    { method: "GET" },
  );
}

export async function approveJournal(
  entityId: string,
  closeRunId: string,
  journalId: string,
  reason?: string | null,
): Promise<void> {
  await requestJson(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "journals", journalId, "approve"]),
    {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
}

export async function applyJournal(
  entityId: string,
  closeRunId: string,
  journalId: string,
  postingTarget: JournalPostingTarget,
  reason?: string | null,
): Promise<void> {
  await requestJson(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "journals", journalId, "apply"]),
    {
      method: "POST",
      body: JSON.stringify({
        posting_target: postingTarget,
        reason: reason ?? null,
      }),
    },
  );
}

export function buildJournalPostingDownloadPath(
  entityId: string,
  closeRunId: string,
  journalId: string,
  postingId: string,
): string {
  return buildEntityProxyPath(entityId, [
    "close-runs",
    closeRunId,
    "journals",
    journalId,
    "postings",
    postingId,
    "download",
  ]);
}

export async function rejectJournal(
  entityId: string,
  closeRunId: string,
  journalId: string,
  reason: string,
): Promise<void> {
  await requestJson(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "journals", journalId, "reject"]),
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
  );
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizeRequestMethod(value: string | null | undefined): string {
  return (value ?? "GET").toUpperCase();
}
