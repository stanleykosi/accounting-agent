/*
Purpose: Centralize same-origin imported-ledger API helpers for the desktop UI.
Scope: Entity-level ledger workspace reads plus GL/TB upload calls through the existing entity proxy.
Dependencies: Browser Fetch APIs and the existing `/api/entities/**` proxy surface.
*/

import {
  buildEntityCacheInvalidationPrefixes,
  invalidateClientCacheByPrefix,
  loadClientCachedValue,
} from "./client-cache";
import { buildEntityProxyPath } from "./entity-proxy";

export type CloseRunLedgerBindingSummary = {
  bound_by_user_id: string | null;
  binding_source: string;
  close_run_id: string;
  created_at: string;
  general_ledger_import_batch_id: string | null;
  trial_balance_import_batch_id: string | null;
  updated_at: string;
};

export type GeneralLedgerImportSummary = {
  created_at: string;
  entity_id: string;
  id: string;
  import_metadata: Record<string, unknown>;
  imported_by_user_id: string | null;
  period_end: string;
  period_start: string;
  row_count: number;
  source_format: string;
  updated_at: string;
  uploaded_filename: string;
};

export type TrialBalanceImportSummary = GeneralLedgerImportSummary;

export type GeneralLedgerExportSummary = {
  adjustment_line_count: number;
  artifact_id: string;
  checksum: string;
  close_run_id: string;
  composition_mode: string;
  content_type: string;
  filename: string;
  generated_at: string;
  idempotency_key: string;
  imported_line_count: number;
  includes_imported_baseline: boolean;
  period_end: string;
  period_start: string;
  row_count: number;
  size_bytes: number;
  storage_key: string;
  version_no: number;
};

export type LedgerWorkspaceResponse = {
  close_run_bindings: readonly CloseRunLedgerBindingSummary[];
  general_ledger_imports: readonly GeneralLedgerImportSummary[];
  trial_balance_imports: readonly TrialBalanceImportSummary[];
};

export type LedgerImportUploadResponse = {
  auto_bound_close_run_ids: readonly string[];
  imported_batch: GeneralLedgerImportSummary;
  skipped_close_run_ids: readonly string[];
  workspace: LedgerWorkspaceResponse;
};

export type LedgerApiErrorCode =
  | "access_denied"
  | "close_run_not_found"
  | "general_ledger_export_not_found"
  | "entity_archived"
  | "entity_not_found"
  | "integrity_conflict"
  | "invalid_gl_file"
  | "invalid_tb_file"
  | "no_ledger_data"
  | "session_expired"
  | "session_required"
  | "unknown_error"
  | "unsupported_file_type"
  | "user_disabled"
  | "validation_error";

export class LedgerApiError extends Error {
  readonly code: LedgerApiErrorCode;
  readonly statusCode: number;

  constructor(
    options: Readonly<{ code: LedgerApiErrorCode; message: string; statusCode: number }>,
  ) {
    super(options.message);
    this.name = "LedgerApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

export async function readLedgerWorkspace(entityId: string): Promise<LedgerWorkspaceResponse> {
  return ledgerRequest<LedgerWorkspaceResponse>(buildEntityProxyPath(entityId, ["ledger"]), {
    method: "GET",
  });
}

export async function uploadGeneralLedger(
  entityId: string,
  options: Readonly<{ file: File; periodEnd: string; periodStart: string }>,
): Promise<LedgerImportUploadResponse> {
  const formData = new FormData();
  formData.append("file", options.file);
  formData.append("period_start", options.periodStart);
  formData.append("period_end", options.periodEnd);

  return ledgerRequest<LedgerImportUploadResponse>(
    buildEntityProxyPath(entityId, ["ledger", "general-ledger", "upload"]),
    {
      body: formData,
      method: "POST",
    },
  );
}

export async function uploadTrialBalance(
  entityId: string,
  options: Readonly<{ file: File; periodEnd: string; periodStart: string }>,
): Promise<LedgerImportUploadResponse> {
  const formData = new FormData();
  formData.append("file", options.file);
  formData.append("period_start", options.periodStart);
  formData.append("period_end", options.periodEnd);

  return ledgerRequest<LedgerImportUploadResponse>(
    buildEntityProxyPath(entityId, ["ledger", "trial-balance", "upload"]),
    {
      body: formData,
      method: "POST",
    },
  );
}

export async function generateGeneralLedgerExport(
  entityId: string,
  closeRunId: string,
): Promise<GeneralLedgerExportSummary> {
  return ledgerRequest<GeneralLedgerExportSummary>(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "ledger", "general-ledger-export"]),
    {
      body: JSON.stringify({}),
      method: "POST",
    },
  );
}

export async function readLatestGeneralLedgerExport(
  entityId: string,
  closeRunId: string,
): Promise<GeneralLedgerExportSummary | null> {
  try {
    return await ledgerRequest<GeneralLedgerExportSummary>(
      buildEntityProxyPath(entityId, ["close-runs", closeRunId, "ledger", "general-ledger-export"]),
      {
        method: "GET",
      },
    );
  } catch (error: unknown) {
    if (
      error instanceof LedgerApiError &&
      error.statusCode === 404 &&
      error.code === "general_ledger_export_not_found"
    ) {
      return null;
    }
    throw error;
  }
}

export function buildGeneralLedgerExportDownloadPath(entityId: string, closeRunId: string): string {
  return buildEntityProxyPath(entityId, [
    "close-runs",
    closeRunId,
    "ledger",
    "general-ledger-export",
    "download",
  ]);
}

async function ledgerRequest<TResponse>(
  path: string,
  init: Readonly<RequestInit>,
): Promise<TResponse> {
  const isFormDataBody = init.body instanceof FormData;
  const requestMethod = normalizeRequestMethod(init.method);

  const performRequest = async (): Promise<TResponse> => {
    const response = await fetch(path, {
      ...init,
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        ...(init.body && !isFormDataBody ? { "Content-Type": "application/json" } : {}),
        ...init.headers,
      },
    });

    const payload = await parseJsonPayload(response);
    if (!response.ok) {
      throw buildLedgerApiError(response.status, payload);
    }

    return payload as TResponse;
  };

  if (requestMethod === "GET") {
    return loadClientCachedValue(path, performRequest);
  }

  const payload = await performRequest();
  invalidateClientCacheByPrefix(buildEntityCacheInvalidationPrefixes(path));
  return payload;
}

function buildLedgerApiError(statusCode: number, payload: unknown): LedgerApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      return new LedgerApiError({
        code: asLedgerApiErrorCode(detail.code),
        message:
          typeof detail.message === "string"
            ? detail.message
            : "The imported-ledger request could not be completed.",
        statusCode,
      });
    }
    if (Array.isArray(detail)) {
      return new LedgerApiError({
        code: "validation_error",
        message: "Review the imported-ledger fields and try again.",
        statusCode,
      });
    }
  }

  return new LedgerApiError({
    code: "unknown_error",
    message: "The imported-ledger request failed. Reload the page and try again.",
    statusCode,
  });
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

function asLedgerApiErrorCode(value: unknown): LedgerApiErrorCode {
  switch (value) {
    case "access_denied":
    case "close_run_not_found":
    case "general_ledger_export_not_found":
    case "entity_archived":
    case "entity_not_found":
    case "integrity_conflict":
    case "invalid_gl_file":
    case "invalid_tb_file":
    case "no_ledger_data":
    case "session_expired":
    case "session_required":
    case "unsupported_file_type":
    case "user_disabled":
      return value;
    default:
      return "unknown_error";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function normalizeRequestMethod(value: string | null | undefined): string {
  return (value ?? "GET").toUpperCase();
}
