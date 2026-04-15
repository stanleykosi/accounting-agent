/*
Purpose: Centralize same-origin report-template API helpers for the desktop UI.
Scope: Template listing, creation, activation, guardrail validation, and commentary reads.
Dependencies: Browser Fetch APIs and the existing `/api/entities/**` proxy surface.
*/

export type ReportSectionDefinition = {
  section_key: string;
  label: string;
  display_order: number;
  is_required: boolean;
  section_config: Record<string, unknown>;
};

export type ReportTemplateSummary = {
  id: string;
  entity_id: string | null;
  source: string;
  version_no: number;
  name: string;
  description: string | null;
  is_active: boolean;
  section_count: number;
  has_required_sections: boolean;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ReportTemplateDetail = ReportTemplateSummary & {
  sections: readonly ReportSectionDefinition[];
  guardrail_config: Record<string, unknown>;
};

export type ReportRunSummary = {
  id: string;
  close_run_id: string;
  template_id: string;
  version_no: number;
  status: string;
  failure_reason: string | null;
  generated_by_user_id: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
};

export type CommentarySummary = {
  id: string;
  report_run_id: string;
  section_key: string;
  status: string;
  body: string;
  authored_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

export type CreateReportTemplateRequest = {
  name: string;
  description: string | null;
  sections: ReportSectionDefinition[];
  guardrail_config: Record<string, unknown>;
  activate_immediately: boolean;
};

export type GuardrailViolation = {
  violation_type: string;
  section_key: string | null;
  message: string;
};

export type GuardrailValidationResponse = {
  template_id: string;
  is_valid: boolean;
  violations: readonly GuardrailViolation[];
};

export type ReportTemplateListResponse = {
  entity_id: string;
  templates: readonly ReportTemplateSummary[];
  active_template_id: string | null;
};

export type ReportApiErrorCode =
  | "entity_not_found"
  | "entity_archived"
  | "template_not_found"
  | "template_not_active"
  | "template_guardrail_violation"
  | "report_run_not_found"
  | "commentary_not_found"
  | "commentary_already_approved"
  | "session_expired"
  | "session_required"
  | "unexpected_error";

export class ReportApiError extends Error {
  constructor(
    message: string,
    public readonly code: ReportApiErrorCode,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ReportApiError";
  }
}

/* ------------------------------------------------------------------ */
/* HTTP helpers                                                        */
/* ------------------------------------------------------------------ */

function apiPath(entityId: string, suffix: string): string {
  const base = suffix.startsWith("/") ? suffix : `/${suffix}`;
  return `/api/entities/${entityId}/reports${base}`;
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
    credentials: "include",
  });

  if (!response.ok) {
    let detail: { code?: string; message?: string } | null = null;
    try {
      detail = (await response.json()) as { code?: string; message?: string };
    } catch {
      // Ignore JSON parse errors on error responses.
    }

    throw new ReportApiError(
      detail?.message ?? `Request failed with status ${response.status}.`,
      (detail?.code as ReportApiErrorCode) ?? "unexpected_error",
      response.status,
    );
  }

  return (await response.json()) as T;
}

/* ------------------------------------------------------------------ */
/* Template queries                                                    */
/* ------------------------------------------------------------------ */

export async function listReportTemplates(
  entityId: string,
): Promise<ReportTemplateListResponse> {
  return requestJson<ReportTemplateListResponse>(apiPath(entityId, "/templates"));
}

export async function readReportTemplate(
  entityId: string,
  templateId: string,
): Promise<ReportTemplateDetail> {
  return requestJson<ReportTemplateDetail>(
    apiPath(entityId, `/templates/${templateId}`),
  );
}

/* ------------------------------------------------------------------ */
/* Template mutations                                                  */
/* ------------------------------------------------------------------ */

export async function createReportTemplate(
  entityId: string,
  payload: CreateReportTemplateRequest,
): Promise<ReportTemplateDetail> {
  return requestJson<ReportTemplateDetail>(apiPath(entityId, "/templates"), {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function activateReportTemplate(
  entityId: string,
  templateId: string,
  reason?: string | null,
): Promise<ReportTemplateDetail> {
  return requestJson<ReportTemplateDetail>(
    apiPath(entityId, `/templates/${templateId}/activate`),
    {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    },
  );
}

export async function validateReportTemplateGuardrails(
  entityId: string,
  templateId: string,
): Promise<GuardrailValidationResponse> {
  return requestJson<GuardrailValidationResponse>(
    apiPath(entityId, `/templates/${templateId}/validate`),
  );
}

/* ------------------------------------------------------------------ */
/* Commentary queries                                                  */
/* ------------------------------------------------------------------ */

export async function updateReportCommentary(
  entityId: string,
  closeRunId: string,
  reportRunId: string,
  sectionKey: string,
  body: string,
): Promise<CommentarySummary> {
  return requestJson<CommentarySummary>(
    apiPath(
      entityId,
      `/close-runs/${closeRunId}/runs/${reportRunId}/commentary/${sectionKey}`,
    ),
    {
      method: "PUT",
      body: JSON.stringify({ body }),
    },
  );
}

export async function approveReportCommentary(
  entityId: string,
  closeRunId: string,
  reportRunId: string,
  sectionKey: string,
  body?: string | null,
  reason?: string | null,
): Promise<CommentarySummary> {
  return requestJson<CommentarySummary>(
    apiPath(
      entityId,
      `/close-runs/${closeRunId}/runs/${reportRunId}/commentary/${sectionKey}/approve`,
    ),
    {
      method: "POST",
      body: JSON.stringify({ body: body ?? null, reason: reason ?? null }),
    },
  );
}

export async function generateReportRun(
  entityId: string,
  closeRunId: string,
  options?: {
    generateCommentary?: boolean;
    useLlmCommentary?: boolean;
  },
): Promise<ReportRunSummary> {
  const searchParams = new URLSearchParams();
  searchParams.set(
    "generate_commentary",
    String(options?.generateCommentary ?? true),
  );
  searchParams.set(
    "use_llm_commentary",
    String(options?.useLlmCommentary ?? false),
  );
  return requestJson<ReportRunSummary>(
    apiPath(entityId, `/close-runs/${closeRunId}/generate?${searchParams.toString()}`),
    {
      method: "POST",
    },
  );
}

export async function listReportRuns(
  entityId: string,
  closeRunId: string,
): Promise<readonly ReportRunSummary[]> {
  const payload = await requestJson<{ report_runs: ReportRunSummary[] }>(
    apiPath(entityId, `/close-runs/${closeRunId}/runs`),
  );
  return payload.report_runs;
}

export async function readReportRun(
  entityId: string,
  closeRunId: string,
  reportRunId: string,
): Promise<ReportRunSummary & { artifact_refs: readonly Record<string, unknown>[]; commentary: readonly CommentarySummary[] }> {
  return requestJson<
    ReportRunSummary & {
      artifact_refs: readonly Record<string, unknown>[];
      commentary: readonly CommentarySummary[];
    }
  >(apiPath(entityId, `/close-runs/${closeRunId}/runs/${reportRunId}`));
}

export function buildReportArtifactDownloadPath(
  entityId: string,
  closeRunId: string,
  reportRunId: string,
  artifactType: string,
): string {
  return apiPath(
    entityId,
    `/close-runs/${closeRunId}/runs/${reportRunId}/artifacts/${encodeURIComponent(artifactType)}`,
  );
}
