/*
Purpose: Provide same-origin background job helpers for hosted operator monitoring screens.
Scope: Job listing, detail reads, cancellation, and resume controls.
Dependencies: Browser Fetch APIs and existing `/api/entities/**` proxy routes.
*/

export type JobSummary = {
  attempt_count: number;
  blocking_reason: string | null;
  cancellation_requested_at: string | null;
  close_run_id: string | null;
  completed_at: string | null;
  created_at: string;
  dead_lettered_at: string | null;
  document_id: string | null;
  entity_id: string | null;
  failure_reason: string | null;
  id: string;
  max_retries: number;
  queue_name: string;
  resumed_from_job_id: string | null;
  retry_count: number;
  routing_key: string;
  started_at: string | null;
  status: string;
  task_name: string;
  updated_at: string;
};

export type JobDetail = JobSummary & {
  actor_user_id: string | null;
  canceled_by_user_id: string | null;
  checkpoint_payload: Record<string, unknown>;
  failure_details: Record<string, unknown> | null;
  payload: Record<string, unknown>;
  result_payload: Record<string, unknown> | null;
  trace_id: string | null;
};

export class JobApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code: string | null = null,
  ) {
    super(message);
    this.name = "JobApiError";
  }
}

function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `/api/entities/${encodedSegments.join("/")}`;
}

async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
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
    throw new JobApiError(
      isRecord(detail) && typeof detail.message === "string"
        ? detail.message
        : `Request failed with status ${response.status}.`,
      response.status,
      isRecord(detail) && typeof detail.code === "string" ? detail.code : null,
    );
  }
  return payload as T;
}

export async function listEntityJobs(
  entityId: string,
  options?: { closeRunId?: string },
): Promise<readonly JobSummary[]> {
  const searchParams = new URLSearchParams();
  if (options?.closeRunId) {
    searchParams.set("close_run_id", options.closeRunId);
  }
  const suffix = searchParams.size > 0 ? `?${searchParams.toString()}` : "";
  const payload = await requestJson<{ jobs: JobSummary[] }>(
    `${buildEntityProxyPath(entityId, ["jobs"])}${suffix}`,
    { method: "GET" },
  );
  return payload.jobs;
}

export async function readJobDetail(entityId: string, jobId: string): Promise<JobDetail> {
  return requestJson<JobDetail>(buildEntityProxyPath(entityId, ["jobs", jobId]), {
    method: "GET",
  });
}

export async function cancelJob(entityId: string, jobId: string, reason: string): Promise<void> {
  await requestJson(buildEntityProxyPath(entityId, ["jobs", jobId, "cancel"]), {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export async function resumeJob(entityId: string, jobId: string, reason: string): Promise<void> {
  await requestJson(buildEntityProxyPath(entityId, ["jobs", jobId, "resume"]), {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
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
