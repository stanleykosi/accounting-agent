/*
Purpose: Centralize same-origin document review workspace data access and derivation for the desktop UI.
Scope: Document queue reads, close-run period context reads, typed API errors, and exception/evidence summaries.
Dependencies: Browser Fetch APIs, existing `/api/entities/**` proxy routes, and strict runtime response guards.
*/

export type DocumentReviewFilter =
  | "all"
  | "low_confidence"
  | "blocked"
  | "duplicate"
  | "wrong_period";

export type ReviewDraftDecision = "approved" | "rejected" | "needs_info";

export type DocumentReviewIssueType = Exclude<DocumentReviewFilter, "all">;

export type DocumentReviewIssueSeverity = "warning" | "blocking";

export type DocumentConfidenceBand = "high" | "medium" | "low" | "unknown";

export type DocumentPeriodState = "in_period" | "out_of_period" | "unknown";

export type DocumentStatus =
  | "uploaded"
  | "processing"
  | "parsed"
  | "needs_review"
  | "approved"
  | "rejected"
  | "failed"
  | "duplicate"
  | "blocked";

export type DocumentType =
  | "unknown"
  | "invoice"
  | "bank_statement"
  | "payslip"
  | "receipt"
  | "contract";

export type EvidenceReference = {
  confidence: number | null;
  id: string;
  kind: "classification" | "period_validation" | "source_metadata" | "workflow_state";
  label: string;
  location: string;
  snippet: string | null;
};

export type ExtractionFieldSummary = {
  confidence: number | null;
  evidenceRefs: readonly EvidenceReference[];
  id: string;
  label: string;
  value: string;
};

export type DocumentReviewQueueItem = {
  classificationConfidence: number | null;
  closeRunId: string;
  createdAt: string;
  documentType: DocumentType;
  fileSizeBytes: number;
  hasException: boolean;
  id: string;
  issueSeverity: DocumentReviewIssueSeverity | null;
  issueTypes: readonly DocumentReviewIssueType[];
  mimeType: string;
  ocrRequired: boolean;
  originalFilename: string;
  periodEnd: string | null;
  periodStart: string | null;
  periodState: DocumentPeriodState;
  sha256Hash: string;
  sourceChannel: "upload" | "api_import" | "manual_entry";
  status: DocumentStatus;
  storageKey: string;
  updatedAt: string;
  confidenceBand: DocumentConfidenceBand;
  evidenceRefs: readonly EvidenceReference[];
  extractedFields: readonly ExtractionFieldSummary[];
};

export type DocumentReviewQueueCounts = Record<DocumentReviewFilter, number>;

export type DocumentReviewWorkspaceData = {
  closeRunId: string;
  closeRunPeriodEnd: string;
  closeRunPeriodStart: string;
  closeRunStatus: string;
  confidenceThreshold: number;
  entityId: string;
  items: readonly DocumentReviewQueueItem[];
  queueCounts: DocumentReviewQueueCounts;
};

export type DocumentReviewApiErrorCode =
  | "close_run_not_found"
  | "entity_not_found"
  | "session_expired"
  | "session_required"
  | "unknown_error"
  | "user_disabled"
  | "validation_error";

/**
 * Purpose: Represent a structured document-review API failure that UI callers can branch on.
 * Inputs: Stable error code, HTTP status, and operator-facing message returned by the API boundary.
 * Outputs: A typed Error instance carrying both human and machine-readable diagnostics.
 * Behavior: Preserves fail-fast API messages so operators can take explicit recovery steps.
 */
export class DocumentReviewApiError extends Error {
  readonly code: DocumentReviewApiErrorCode;
  readonly statusCode: number;

  constructor(
    options: Readonly<{ code: DocumentReviewApiErrorCode; message: string; statusCode: number }>,
  ) {
    super(options.message);
    this.name = "DocumentReviewApiError";
    this.code = options.code;
    this.statusCode = options.statusCode;
  }
}

const DEFAULT_CLASSIFICATION_THRESHOLD = 0.75;
const ENTITIES_PROXY_BASE_PATH = "/api/entities";

/**
 * Purpose: Load and normalize the document review workspace for one entity close run.
 * Inputs: Entity UUID and close-run UUID from the active route context.
 * Outputs: Typed queue data with exception categories, evidence references, and queue counts.
 * Behavior: Fetches close-run metadata and document rows in parallel, then derives review state deterministically.
 */
export async function readDocumentReviewWorkspace(
  entityId: string,
  closeRunId: string,
): Promise<DocumentReviewWorkspaceData> {
  const [closeRunPayload, documentsPayload] = await Promise.all([
    documentReviewRequest<unknown>(buildEntityProxyPath(entityId, ["close-runs", closeRunId]), {
      method: "GET",
    }),
    documentReviewRequest<unknown>(
      buildEntityProxyPath(entityId, ["close-runs", closeRunId, "documents"]),
      {
        method: "GET",
      },
    ),
  ]);

  const closeRun = parseCloseRunSummary(closeRunPayload);
  const documents = parseDocumentSummaryList(documentsPayload);
  const items = documents.map((document) =>
    buildQueueItem({
      closeRunPeriodEnd: closeRun.periodEnd,
      closeRunPeriodStart: closeRun.periodStart,
      confidenceThreshold: DEFAULT_CLASSIFICATION_THRESHOLD,
      document,
    }),
  );

  return {
    closeRunId: closeRun.id,
    closeRunPeriodEnd: closeRun.periodEnd,
    closeRunPeriodStart: closeRun.periodStart,
    closeRunStatus: closeRun.status,
    confidenceThreshold: DEFAULT_CLASSIFICATION_THRESHOLD,
    entityId,
    items,
    queueCounts: buildQueueCounts(items),
  };
}

/**
 * Purpose: Filter document review queue items by one exception-focused view.
 * Inputs: Full queue items and the selected filter value.
 * Outputs: Deterministically ordered item subset for the active table view.
 * Behavior: Keeps `all` as a pass-through and applies explicit category predicates for every other filter.
 */
export function filterDocumentReviewItems(
  items: readonly DocumentReviewQueueItem[],
  filter: DocumentReviewFilter,
): readonly DocumentReviewQueueItem[] {
  if (filter === "all") {
    return items;
  }

  return items.filter((item) => item.issueTypes.includes(filter));
}

/**
 * Purpose: Format an ISO period pair into one operator-facing compact label.
 * Inputs: Period start and end values from a close run.
 * Outputs: Readable period text for headers and summary cards.
 * Behavior: Falls back to the raw period strings when a date cannot be parsed.
 */
export function formatPeriodLabel(periodStart: string, periodEnd: string): string {
  const start = safeParseDate(periodStart);
  const end = safeParseDate(periodEnd);
  if (start === null || end === null) {
    return `${periodStart} to ${periodEnd}`;
  }

  return `${start.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  })} to ${end.toLocaleDateString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  })}`;
}

/**
 * Purpose: Convert a queue item confidence score to a compact percentage label.
 * Inputs: Optional confidence value in [0, 1].
 * Outputs: Human-readable percentage text or an explicit unknown marker.
 * Behavior: Rounds to whole percentages so queue rows remain compact.
 */
export function formatConfidenceLabel(score: number | null): string {
  if (score === null) {
    return "Unknown";
  }

  return `${Math.round(score * 100)}%`;
}

async function documentReviewRequest<TResponse>(
  path: string,
  init: Readonly<RequestInit>,
): Promise<TResponse> {
  const response = await fetch(path, {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  const payload = await parseJsonPayload(response);
  if (!response.ok) {
    throw buildDocumentReviewApiError(response.status, payload);
  }

  return payload as TResponse;
}

function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `${ENTITIES_PROXY_BASE_PATH}/${encodedSegments.join("/")}`;
}

function buildDocumentReviewApiError(
  statusCode: number,
  payload: unknown,
): DocumentReviewApiError {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (isRecord(detail)) {
      const message =
        typeof detail.message === "string"
          ? detail.message
          : "The document review request could not be completed.";
      return new DocumentReviewApiError({
        code: asDocumentReviewApiErrorCode(detail.code),
        message,
        statusCode,
      });
    }

    if (Array.isArray(detail)) {
      return new DocumentReviewApiError({
        code: "validation_error",
        message: "Review the selected entity and close-run identifiers, then retry.",
        statusCode,
      });
    }
  }

  return new DocumentReviewApiError({
    code: "unknown_error",
    message: "The document review request failed. Reload and try again.",
    statusCode,
  });
}

function asDocumentReviewApiErrorCode(value: unknown): DocumentReviewApiErrorCode {
  switch (value) {
    case "close_run_not_found":
    case "entity_not_found":
    case "session_expired":
    case "session_required":
    case "user_disabled":
      return value;
    default:
      return "unknown_error";
  }
}

function parseCloseRunSummary(payload: unknown): {
  id: string;
  periodEnd: string;
  periodStart: string;
  status: string;
} {
  if (!isRecord(payload)) {
    throw new Error("Close-run response was not an object.");
  }

  return {
    id: requireString(payload.id, "closeRun.id"),
    periodEnd: requireString(payload.period_end, "closeRun.period_end"),
    periodStart: requireString(payload.period_start, "closeRun.period_start"),
    status: requireString(payload.status, "closeRun.status"),
  };
}

function parseDocumentSummaryList(payload: unknown): readonly DocumentApiSummary[] {
  if (!isRecord(payload)) {
    throw new Error("Document-list response was not an object.");
  }

  const documents = payload.documents;
  if (!Array.isArray(documents)) {
    throw new Error("Document-list response is missing a documents array.");
  }

  return documents.map((item, index) => parseDocumentSummary(item, index));
}

function parseDocumentSummary(value: unknown, index: number): DocumentApiSummary {
  if (!isRecord(value)) {
    throw new Error(`Document row ${index + 1} was not an object.`);
  }

  return {
    classificationConfidence: requireNullableNumber(
      value.classification_confidence,
      `documents[${index}].classification_confidence`,
    ),
    closeRunId: requireString(value.close_run_id, `documents[${index}].close_run_id`),
    createdAt: requireString(value.created_at, `documents[${index}].created_at`),
    documentType: requireDocumentType(value.document_type, `documents[${index}].document_type`),
    fileSizeBytes: requireNumber(value.file_size_bytes, `documents[${index}].file_size_bytes`),
    id: requireString(value.id, `documents[${index}].id`),
    mimeType: requireString(value.mime_type, `documents[${index}].mime_type`),
    ocrRequired: requireBoolean(value.ocr_required, `documents[${index}].ocr_required`),
    originalFilename: requireString(value.original_filename, `documents[${index}].original_filename`),
    periodEnd: requireNullableString(value.period_end, `documents[${index}].period_end`),
    periodStart: requireNullableString(value.period_start, `documents[${index}].period_start`),
    sha256Hash: requireString(value.sha256_hash, `documents[${index}].sha256_hash`),
    sourceChannel: requireSourceChannel(value.source_channel, `documents[${index}].source_channel`),
    status: requireDocumentStatus(value.status, `documents[${index}].status`),
    storageKey: requireString(value.storage_key, `documents[${index}].storage_key`),
    updatedAt: requireString(value.updated_at, `documents[${index}].updated_at`),
  };
}

function buildQueueItem(options: {
  closeRunPeriodEnd: string;
  closeRunPeriodStart: string;
  confidenceThreshold: number;
  document: DocumentApiSummary;
}): DocumentReviewQueueItem {
  const { document } = options;
  const periodState = resolvePeriodState(
    document.periodStart,
    document.periodEnd,
    options.closeRunPeriodStart,
    options.closeRunPeriodEnd,
  );

  const issueTypes: DocumentReviewIssueType[] = [];

  const lowConfidence =
    document.status === "needs_review" ||
    (document.classificationConfidence !== null &&
      document.classificationConfidence < options.confidenceThreshold);
  if (lowConfidence) {
    issueTypes.push("low_confidence");
  }

  if (document.status === "blocked" || document.status === "failed") {
    issueTypes.push("blocked");
  }

  if (document.status === "duplicate") {
    issueTypes.push("duplicate");
  }

  if (periodState === "out_of_period") {
    issueTypes.push("wrong_period");
  }

  const issueSeverity = resolveIssueSeverity(issueTypes);
  const evidenceRefs = buildEvidenceRefs(document, periodState, lowConfidence);

  return {
    classificationConfidence: document.classificationConfidence,
    closeRunId: document.closeRunId,
    confidenceBand: resolveConfidenceBand(
      document.classificationConfidence,
      options.confidenceThreshold,
    ),
    createdAt: document.createdAt,
    documentType: document.documentType,
    evidenceRefs,
    extractedFields: buildExtractionFieldSummaries(document, periodState, evidenceRefs),
    fileSizeBytes: document.fileSizeBytes,
    hasException: issueTypes.length > 0,
    id: document.id,
    issueSeverity,
    issueTypes,
    mimeType: document.mimeType,
    ocrRequired: document.ocrRequired,
    originalFilename: document.originalFilename,
    periodEnd: document.periodEnd,
    periodStart: document.periodStart,
    periodState,
    sha256Hash: document.sha256Hash,
    sourceChannel: document.sourceChannel,
    status: document.status,
    storageKey: document.storageKey,
    updatedAt: document.updatedAt,
  };
}

function buildQueueCounts(items: readonly DocumentReviewQueueItem[]): DocumentReviewQueueCounts {
  return {
    all: items.length,
    blocked: items.filter((item) => item.issueTypes.includes("blocked")).length,
    duplicate: items.filter((item) => item.issueTypes.includes("duplicate")).length,
    low_confidence: items.filter((item) => item.issueTypes.includes("low_confidence")).length,
    wrong_period: items.filter((item) => item.issueTypes.includes("wrong_period")).length,
  };
}

function resolveIssueSeverity(
  issueTypes: readonly DocumentReviewIssueType[],
): DocumentReviewIssueSeverity | null {
  if (issueTypes.length === 0) {
    return null;
  }

  if (
    issueTypes.includes("blocked") ||
    issueTypes.includes("duplicate") ||
    issueTypes.includes("wrong_period")
  ) {
    return "blocking";
  }

  return "warning";
}

function resolvePeriodState(
  periodStart: string | null,
  periodEnd: string | null,
  closeRunPeriodStart: string,
  closeRunPeriodEnd: string,
): DocumentPeriodState {
  const closeStart = safeParseDate(closeRunPeriodStart);
  const closeEnd = safeParseDate(closeRunPeriodEnd);
  if (closeStart === null || closeEnd === null) {
    return "unknown";
  }

  const documentStart = safeParseDate(periodStart);
  const documentEnd = safeParseDate(periodEnd);
  if (documentStart === null && documentEnd === null) {
    return "unknown";
  }

  if ((documentStart !== null && documentStart > closeEnd) || (documentEnd !== null && documentEnd < closeStart)) {
    return "out_of_period";
  }

  return "in_period";
}

function resolveConfidenceBand(
  score: number | null,
  threshold: number,
): DocumentConfidenceBand {
  if (score === null) {
    return "unknown";
  }

  if (score >= 0.9) {
    return "high";
  }

  if (score >= threshold) {
    return "medium";
  }

  return "low";
}

function buildEvidenceRefs(
  document: DocumentApiSummary,
  periodState: DocumentPeriodState,
  lowConfidence: boolean,
): readonly EvidenceReference[] {
  const references: EvidenceReference[] = [
    {
      confidence: null,
      id: `${document.id}:source:file`,
      kind: "source_metadata",
      label: "Source file",
      location: document.originalFilename,
      snippet: `MIME ${document.mimeType} • ${formatByteSize(document.fileSizeBytes)}`,
    },
    {
      confidence: null,
      id: `${document.id}:source:key`,
      kind: "source_metadata",
      label: "Object key",
      location: document.storageKey,
      snippet: `Checksum ${document.sha256Hash.slice(0, 12)}...`,
    },
    {
      confidence: document.classificationConfidence,
      id: `${document.id}:classification`,
      kind: "classification",
      label: "Classification confidence",
      location: "document.classification_confidence",
      snippet:
        document.classificationConfidence === null
          ? "Classification confidence has not been produced yet."
          : `Model confidence ${formatConfidenceLabel(document.classificationConfidence)} for ${formatLabelValue(document.documentType)}.`,
    },
  ];

  if (periodState === "out_of_period") {
    references.push({
      confidence: null,
      id: `${document.id}:period:outside`,
      kind: "period_validation",
      label: "Period mismatch",
      location: "document.period_start / document.period_end",
      snippet: `Detected period ${formatDetectedPeriod(document.periodStart, document.periodEnd)} is outside this close run window.`,
    });
  }

  if (document.status === "duplicate") {
    references.push({
      confidence: null,
      id: `${document.id}:workflow:duplicate`,
      kind: "workflow_state",
      label: "Duplicate status",
      location: "document.status",
      snippet: "This document is flagged as a duplicate and requires reviewer disposition.",
    });
  }

  if (document.status === "blocked" || document.status === "failed") {
    references.push({
      confidence: null,
      id: `${document.id}:workflow:blocked`,
      kind: "workflow_state",
      label: "Blocked workflow state",
      location: "document.status",
      snippet: "This document is blocked and cannot progress until an explicit recovery action is taken.",
    });
  }

  if (lowConfidence) {
    references.push({
      confidence: document.classificationConfidence,
      id: `${document.id}:workflow:low-confidence`,
      kind: "workflow_state",
      label: "Low-confidence routing",
      location: "review queue policy",
      snippet: "The document entered review because confidence fell below the configured threshold.",
    });
  }

  return references;
}

function buildExtractionFieldSummaries(
  document: DocumentApiSummary,
  periodState: DocumentPeriodState,
  evidenceRefs: readonly EvidenceReference[],
): readonly ExtractionFieldSummary[] {
  const classificationEvidence = evidenceRefs.filter(
    (reference) => reference.kind === "classification" || reference.kind === "workflow_state",
  );
  const metadataEvidence = evidenceRefs.filter((reference) => reference.kind === "source_metadata");
  const periodEvidence = evidenceRefs.filter((reference) => reference.kind === "period_validation");

  return [
    {
      confidence: document.classificationConfidence,
      evidenceRefs: classificationEvidence,
      id: `${document.id}:field:document-type`,
      label: "Document type",
      value: formatLabelValue(document.documentType),
    },
    {
      confidence: null,
      evidenceRefs: metadataEvidence,
      id: `${document.id}:field:source-channel`,
      label: "Source channel",
      value: formatLabelValue(document.sourceChannel),
    },
    {
      confidence: null,
      evidenceRefs: periodEvidence.length > 0 ? periodEvidence : metadataEvidence,
      id: `${document.id}:field:detected-period`,
      label: "Detected period",
      value: formatDetectedPeriod(document.periodStart, document.periodEnd),
    },
    {
      confidence: null,
      evidenceRefs: metadataEvidence,
      id: `${document.id}:field:lifecycle`,
      label: "Lifecycle status",
      value: formatLabelValue(document.status),
    },
    {
      confidence: null,
      evidenceRefs: metadataEvidence,
      id: `${document.id}:field:ocr-required`,
      label: "OCR required",
      value: document.ocrRequired ? "Yes" : "No",
    },
    {
      confidence: null,
      evidenceRefs: metadataEvidence,
      id: `${document.id}:field:period-state`,
      label: "Period validation",
      value:
        periodState === "out_of_period"
          ? "Outside close-run period"
          : periodState === "in_period"
            ? "Within close-run period"
            : "Period not detected",
    },
  ];
}

function formatDetectedPeriod(periodStart: string | null, periodEnd: string | null): string {
  if (periodStart === null && periodEnd === null) {
    return "Not detected";
  }

  if (periodStart !== null && periodEnd !== null) {
    return `${periodStart} to ${periodEnd}`;
  }

  return periodStart ?? periodEnd ?? "Not detected";
}

function formatLabelValue(value: string): string {
  return value
    .split("_")
    .filter((part) => part.length > 0)
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatByteSize(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }

  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }

  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

function safeParseDate(value: string | null): Date | null {
  if (typeof value !== "string" || value.trim().length === 0) {
    return null;
  }

  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type");
  if (contentType === null || !contentType.includes("application/json")) {
    return null;
  }

  return response.json();
}

function requireDocumentStatus(value: unknown, fieldName: string): DocumentStatus {
  switch (value) {
    case "uploaded":
    case "processing":
    case "parsed":
    case "needs_review":
    case "approved":
    case "rejected":
    case "failed":
    case "duplicate":
    case "blocked":
      return value;
    default:
      throw new Error(`${fieldName} must be a supported document status.`);
  }
}

function requireDocumentType(value: unknown, fieldName: string): DocumentType {
  switch (value) {
    case "unknown":
    case "invoice":
    case "bank_statement":
    case "payslip":
    case "receipt":
    case "contract":
      return value;
    default:
      throw new Error(`${fieldName} must be a supported document type.`);
  }
}

function requireSourceChannel(
  value: unknown,
  fieldName: string,
): "upload" | "api_import" | "manual_entry" {
  switch (value) {
    case "upload":
    case "api_import":
    case "manual_entry":
      return value;
    default:
      throw new Error(`${fieldName} must be a supported document source channel.`);
  }
}

function requireBoolean(value: unknown, fieldName: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`${fieldName} must be a boolean.`);
  }

  return value;
}

function requireNumber(value: unknown, fieldName: string): number {
  if (typeof value !== "number" || Number.isNaN(value)) {
    throw new Error(`${fieldName} must be a valid number.`);
  }

  return value;
}

function requireNullableNumber(value: unknown, fieldName: string): number | null {
  if (value === null) {
    return null;
  }

  return requireNumber(value, fieldName);
}

function requireString(value: unknown, fieldName: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${fieldName} must be a non-empty string.`);
  }

  return value;
}

function requireNullableString(value: unknown, fieldName: string): string | null {
  if (value === null) {
    return null;
  }

  return requireString(value, fieldName);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

type DocumentApiSummary = {
  classificationConfidence: number | null;
  closeRunId: string;
  createdAt: string;
  documentType: DocumentType;
  fileSizeBytes: number;
  id: string;
  mimeType: string;
  ocrRequired: boolean;
  originalFilename: string;
  periodEnd: string | null;
  periodStart: string | null;
  sha256Hash: string;
  sourceChannel: "upload" | "api_import" | "manual_entry";
  status: DocumentStatus;
  storageKey: string;
  updatedAt: string;
};
