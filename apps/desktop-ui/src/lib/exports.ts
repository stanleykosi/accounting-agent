/*
Purpose: Provide same-origin export and evidence-pack helpers for hosted close-run release pages.
Scope: Triggering exports, assembling evidence packs, and listing export records.
Dependencies: Browser Fetch APIs and existing `/api/entities/**` proxy routes.
*/

export type ExportSummary = {
  artifact_count: number;
  close_run_id: string;
  completed_at: string | null;
  created_at: string;
  distribution_count: number;
  failure_reason: string | null;
  id: string;
  idempotency_key: string;
  latest_distribution_at: string | null;
  status: string;
  version_no: number;
};

export type EvidencePackItem = {
  checksum: string | null;
  description: string | null;
  item_type: string;
  label: string;
  payload_ref: Record<string, unknown>;
  size_bytes: number | null;
  storage_key: string | null;
};

export type EvidencePackBundle = {
  checksum: string | null;
  close_run_id: string;
  generated_at: string;
  idempotency_key: string;
  items: readonly EvidencePackItem[];
  size_bytes: number | null;
  storage_key: string | null;
  version_no: number;
};

export type ExportArtifactEntry = {
  artifact_type: string;
  checksum: string;
  content_type: string;
  filename: string;
  idempotency_key: string;
  released_at: string;
  size_bytes: number;
  storage_key: string;
};

export type ExportManifest = {
  artifacts: readonly ExportArtifactEntry[];
  close_run_id: string;
  evidence_pack_ref: EvidencePackBundle | null;
  generated_at: string;
  version_no: number;
};

export type ExportDistributionRecord = {
  delivery_channel: string;
  distributed_at: string;
  distributed_by_user_id: string | null;
  id: string;
  note: string | null;
  recipient_email: string;
  recipient_name: string;
  recipient_role: string | null;
};

export type ExportDetail = ExportSummary & {
  distribution_records: readonly ExportDistributionRecord[];
  evidence_pack: EvidencePackBundle | null;
  manifest: ExportManifest | null;
};

export type DistributeExportRequest = {
  delivery_channel: string;
  note?: string | null;
  recipient_email: string;
  recipient_name: string;
  recipient_role?: string | null;
};

export const EXPORT_DELIVERY_CHANNELS = [
  "secure_email",
  "management_portal",
  "board_pack",
  "file_share",
] as const;

export class ExportApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code: string | null = null,
  ) {
    super(message);
    this.name = "ExportApiError";
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
    throw new ExportApiError(
      isRecord(detail) && typeof detail.message === "string"
        ? detail.message
        : `Request failed with status ${response.status}.`,
      response.status,
      isRecord(detail) && typeof detail.code === "string" ? detail.code : null,
    );
  }
  return payload as T;
}

export async function listExports(
  entityId: string,
  closeRunId: string,
): Promise<readonly ExportSummary[]> {
  const payload = await requestJson<{ exports: ExportSummary[] }>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports"]),
    { method: "GET" },
  );
  return payload.exports;
}

export async function triggerExport(entityId: string, closeRunId: string): Promise<ExportDetail> {
  return requestJson<ExportDetail>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports"]),
    {
      method: "POST",
      body: JSON.stringify({ action_qualifier: "full_export" }),
    },
  );
}

export async function readExportDetail(
  entityId: string,
  closeRunId: string,
  exportId: string,
): Promise<ExportDetail> {
  return requestJson<ExportDetail>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports", exportId]),
    { method: "GET" },
  );
}

export async function assembleEvidencePack(
  entityId: string,
  closeRunId: string,
): Promise<EvidencePackBundle> {
  return requestJson<EvidencePackBundle>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports", "evidence-pack"]),
    { method: "POST", body: JSON.stringify({}) },
  );
}

export async function distributeExport(
  entityId: string,
  closeRunId: string,
  exportId: string,
  payload: DistributeExportRequest,
): Promise<ExportDetail> {
  return requestJson<ExportDetail>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports", exportId, "distribute"]),
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export async function readLatestEvidencePack(
  entityId: string,
  closeRunId: string,
): Promise<EvidencePackBundle | null> {
  try {
    return await requestJson<EvidencePackBundle>(
      buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports", "evidence-pack"]),
      { method: "GET" },
    );
  } catch (error) {
    if (
      error instanceof ExportApiError &&
      error.status === 404 &&
      error.code === "evidence_pack_not_found"
    ) {
      return null;
    }
    throw error;
  }
}

export function buildEvidencePackDownloadPath(entityId: string, closeRunId: string): string {
  return buildEntityProxyPath(entityId, ["close-runs", closeRunId, "exports", "evidence-pack", "download"]);
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
