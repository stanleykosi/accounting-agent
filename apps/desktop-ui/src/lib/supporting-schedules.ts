/*
Purpose: Provide same-origin API helpers for standalone Step 6 supporting schedules.
Scope: Read the supporting-schedule workspace and mutate rows or review status
for fixed assets, loan amortisation, accrual tracker, and budget-vs-actual workpapers.
Dependencies: Native fetch and the /api/entities proxy path.
*/

export type SupportingScheduleType =
  | "fixed_assets"
  | "loan_amortisation"
  | "accrual_tracker"
  | "budget_vs_actual";

export type SupportingScheduleStatus =
  | "draft"
  | "in_review"
  | "approved"
  | "not_applicable";

export type FixedAssetRowPayload = {
  accumulated_depreciation: string;
  accumulated_depreciation_account_code: string;
  acquisition_date: string;
  asset_account_code: string;
  asset_id: string;
  asset_name: string;
  cost: string;
  depreciation_expense?: string;
  disposal_date?: string;
  net_book_value?: string;
  notes?: string;
};

export type LoanAmortisationRowPayload = {
  balance: string;
  due_date: string;
  interest: string;
  interest_account_code: string;
  lender_name: string;
  loan_account_code: string;
  loan_id: string;
  notes?: string;
  payment_no: number;
  principal: string;
};

export type AccrualTrackerRowPayload = {
  account_code: string;
  amount: string;
  counterparty?: string;
  description: string;
  notes?: string;
  period: string;
  ref: string;
  reversal_date?: string;
};

export type BudgetVsActualRowPayload = {
  account_code: string;
  budget_amount: string;
  cost_centre?: string;
  department?: string;
  notes?: string;
  period: string;
  project?: string;
};

export type SupportingScheduleRowPayload =
  | FixedAssetRowPayload
  | LoanAmortisationRowPayload
  | AccrualTrackerRowPayload
  | BudgetVsActualRowPayload;

export type SupportingScheduleRowSummary = {
  createdAt: string;
  id: string;
  lineNo: number;
  payload: Record<string, unknown>;
  rowRef: string;
  scheduleId: string;
  scheduleType: SupportingScheduleType;
  updatedAt: string;
};

export type SupportingScheduleSummary = {
  closeRunId: string;
  id: string | null;
  label: string;
  note: string | null;
  reviewedAt: string | null;
  reviewedByUserId: string | null;
  rowCount: number;
  scheduleType: SupportingScheduleType;
  status: SupportingScheduleStatus;
  updatedAt: string | null;
};

export type SupportingScheduleDetail = {
  rows: ReadonlyArray<SupportingScheduleRowSummary>;
  schedule: SupportingScheduleSummary;
};

export type SupportingScheduleWorkspace = {
  schedules: ReadonlyArray<SupportingScheduleDetail>;
};

export class SupportingScheduleApiError extends Error {
  constructor(
    message: string,
    public readonly statusCode: number | null = null,
    public readonly code: string | null = null,
  ) {
    super(message);
    this.name = "SupportingScheduleApiError";
  }
}

const ENTITIES_PROXY_BASE_PATH = "/api/entities";

function buildEntityProxyPath(entityId: string, pathSegments: readonly string[]): string {
  const encodedSegments = [entityId, ...pathSegments].map((segment) => encodeURIComponent(segment));
  return `${ENTITIES_PROXY_BASE_PATH}/${encodedSegments.join("/")}`;
}

function asString(value: unknown, fallback = ""): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asNullableString(value: unknown): string | null {
  const normalized = asString(value);
  return normalized.length > 0 ? normalized : null;
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number") return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? fallback : parsed;
  }
  return fallback;
}

function parseScheduleType(value: unknown): SupportingScheduleType {
  const normalized = asString(value);
  switch (normalized) {
    case "fixed_assets":
    case "loan_amortisation":
    case "accrual_tracker":
    case "budget_vs_actual":
      return normalized;
    default:
      throw new SupportingScheduleApiError(`Unsupported supporting schedule type: ${normalized || "unknown"}.`);
  }
}

function parseScheduleStatus(value: unknown): SupportingScheduleStatus {
  const normalized = asString(value);
  switch (normalized) {
    case "draft":
    case "in_review":
    case "approved":
    case "not_applicable":
      return normalized;
    default:
      throw new SupportingScheduleApiError(`Unsupported supporting schedule status: ${normalized || "unknown"}.`);
  }
}

function parseRowSummary(raw: Record<string, unknown>): SupportingScheduleRowSummary {
  return {
    createdAt: asString(raw.created_at),
    id: asString(raw.id),
    lineNo: asNumber(raw.line_no, 1),
    payload: (raw.payload as Record<string, unknown>) ?? {},
    rowRef: asString(raw.row_ref),
    scheduleId: asString(raw.schedule_id),
    scheduleType: parseScheduleType(raw.schedule_type),
    updatedAt: asString(raw.updated_at),
  };
}

function parseScheduleSummary(raw: Record<string, unknown>): SupportingScheduleSummary {
  return {
    closeRunId: asString(raw.close_run_id),
    id: asNullableString(raw.id),
    label: asString(raw.label),
    note: asNullableString(raw.note),
    reviewedAt: asNullableString(raw.reviewed_at),
    reviewedByUserId: asNullableString(raw.reviewed_by_user_id),
    rowCount: asNumber(raw.row_count),
    scheduleType: parseScheduleType(raw.schedule_type),
    status: parseScheduleStatus(raw.status),
    updatedAt: asNullableString(raw.updated_at),
  };
}

function parseScheduleDetail(raw: Record<string, unknown>): SupportingScheduleDetail {
  const rowsRaw = Array.isArray(raw.rows) ? raw.rows : [];
  return {
    rows: rowsRaw.map((row) => parseRowSummary((row as Record<string, unknown>) ?? {})),
    schedule: parseScheduleSummary((raw.schedule as Record<string, unknown>) ?? {}),
  };
}

async function readJsonOrThrow(response: Response): Promise<Record<string, unknown>> {
  const contentType = response.headers.get("content-type") ?? "";
  const body =
    contentType.includes("application/json") ? ((await response.json()) as Record<string, unknown>) : {};
  if (!response.ok) {
    const detail = (body.detail as Record<string, unknown> | undefined) ?? body;
    throw new SupportingScheduleApiError(
      asString(detail.message, "The supporting schedule request failed."),
      response.status,
      asNullableString(detail.code),
    );
  }
  return body;
}

export async function readSupportingScheduleWorkspace(
  entityId: string,
  closeRunId: string,
): Promise<SupportingScheduleWorkspace> {
  const response = await fetch(
    buildEntityProxyPath(entityId, ["close-runs", closeRunId, "supporting-schedules"]),
    {
      credentials: "include",
      method: "GET",
      headers: {
        Accept: "application/json",
      },
      cache: "no-store",
    },
  );
  const body = await readJsonOrThrow(response);
  const schedulesRaw = Array.isArray(body.schedules) ? body.schedules : [];
  return {
    schedules: schedulesRaw.map((schedule) => parseScheduleDetail((schedule as Record<string, unknown>) ?? {})),
  };
}

export async function saveSupportingScheduleRow(options: {
  entityId: string;
  closeRunId: string;
  scheduleType: SupportingScheduleType;
  rowId?: string;
  payload: SupportingScheduleRowPayload;
}): Promise<SupportingScheduleDetail> {
  const response = await fetch(
    buildEntityProxyPath(options.entityId, [
      "close-runs",
      options.closeRunId,
      "supporting-schedules",
      options.scheduleType,
      "rows",
    ]),
    {
      credentials: "include",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        row_id: options.rowId ?? null,
        payload: options.payload,
      }),
    },
  );
  const body = await readJsonOrThrow(response);
  return parseScheduleDetail((body.schedule as Record<string, unknown>) ?? {});
}

export async function deleteSupportingScheduleRow(options: {
  entityId: string;
  closeRunId: string;
  scheduleType: SupportingScheduleType;
  rowId: string;
}): Promise<SupportingScheduleDetail> {
  const response = await fetch(
    buildEntityProxyPath(options.entityId, [
      "close-runs",
      options.closeRunId,
      "supporting-schedules",
      options.scheduleType,
      "rows",
      options.rowId,
    ]),
    {
      credentials: "include",
      method: "DELETE",
      headers: {
        Accept: "application/json",
      },
    },
  );
  const body = await readJsonOrThrow(response);
  return parseScheduleDetail((body.schedule as Record<string, unknown>) ?? {});
}

export async function updateSupportingScheduleStatus(options: {
  entityId: string;
  closeRunId: string;
  scheduleType: SupportingScheduleType;
  status: Exclude<SupportingScheduleStatus, "draft">;
  note?: string | null;
}): Promise<SupportingScheduleDetail> {
  const response = await fetch(
    buildEntityProxyPath(options.entityId, [
      "close-runs",
      options.closeRunId,
      "supporting-schedules",
      options.scheduleType,
      "status",
    ]),
    {
      credentials: "include",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        status: options.status,
        note: options.note ?? null,
      }),
    },
  );
  const body = await readJsonOrThrow(response);
  return parseScheduleDetail((body.schedule as Record<string, unknown>) ?? {});
}
