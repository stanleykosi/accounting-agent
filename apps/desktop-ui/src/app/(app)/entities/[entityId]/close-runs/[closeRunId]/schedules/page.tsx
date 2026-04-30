/*
Purpose: Render the standalone Step 6 supporting-schedule workspace.
Scope: Fixed asset register, loan amortisation, accrual tracker, and
budget-vs-actual workpaper editors with review-state controls.
Dependencies: Supporting-schedule API helpers and shared surface primitives.
*/

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState, type ChangeEvent, type ReactElement } from "react";
import {
  deleteSupportingScheduleRow,
  readSupportingScheduleWorkspace,
  saveSupportingScheduleRow,
  SupportingScheduleApiError,
  type SupportingScheduleDetail,
  type SupportingScheduleRowPayload,
  type SupportingScheduleStatus,
  type SupportingScheduleType,
  updateSupportingScheduleStatus,
} from "../../../../../../../lib/supporting-schedules";
import { requireRouteParam } from "../../../../../../../lib/route-params";

type ScheduleFieldType = "date" | "month" | "number" | "text" | "textarea";

type ScheduleFieldDefinition = {
  key: string;
  label: string;
  placeholder?: string;
  required?: boolean;
  type: ScheduleFieldType;
};

type ScheduleDefinition = {
  description: string;
  fields: readonly ScheduleFieldDefinition[];
  tableColumns: readonly {
    key: string;
    label: string;
  }[];
};

type DraftState = Record<string, string>;

const SCHEDULE_DEFINITIONS: Readonly<Record<SupportingScheduleType, ScheduleDefinition>> = {
  fixed_assets: {
    description:
      "Maintain the fixed asset register, depreciation accounts, disposals, and net book values used in Step 6.",
    fields: [
      { key: "asset_id", label: "Asset ID", required: true, type: "text" },
      { key: "asset_name", label: "Asset name", required: true, type: "text" },
      { key: "acquisition_date", label: "Acquisition date", required: true, type: "date" },
      { key: "asset_account_code", label: "Asset account code", required: true, type: "text" },
      {
        key: "accumulated_depreciation_account_code",
        label: "Accumulated depreciation account",
        required: true,
        type: "text",
      },
      { key: "cost", label: "Cost", required: true, type: "number" },
      {
        key: "accumulated_depreciation",
        label: "Accumulated depreciation",
        required: true,
        type: "number",
      },
      { key: "depreciation_expense", label: "Period depreciation", type: "number" },
      { key: "disposal_date", label: "Disposal date", type: "date" },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    tableColumns: [
      { key: "asset_id", label: "Asset" },
      { key: "asset_name", label: "Description" },
      { key: "acquisition_date", label: "Acquired" },
      { key: "cost", label: "Cost" },
      { key: "accumulated_depreciation", label: "Acc. dep." },
      { key: "net_book_value", label: "NBV" },
    ],
  },
  loan_amortisation: {
    description:
      "Maintain lender payment schedules, principal and interest splits, and outstanding balances used for reconciliation.",
    fields: [
      { key: "loan_id", label: "Loan ID", required: true, type: "text" },
      { key: "lender_name", label: "Lender", required: true, type: "text" },
      { key: "payment_no", label: "Payment no.", required: true, type: "number" },
      { key: "due_date", label: "Due date", required: true, type: "date" },
      { key: "loan_account_code", label: "Loan account code", required: true, type: "text" },
      {
        key: "interest_account_code",
        label: "Interest account code",
        required: true,
        type: "text",
      },
      { key: "principal", label: "Principal", required: true, type: "number" },
      { key: "interest", label: "Interest", required: true, type: "number" },
      { key: "balance", label: "Balance", required: true, type: "number" },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    tableColumns: [
      { key: "loan_id", label: "Loan" },
      { key: "payment_no", label: "Payment" },
      { key: "due_date", label: "Due" },
      { key: "principal", label: "Principal" },
      { key: "interest", label: "Interest" },
      { key: "balance", label: "Balance" },
    ],
  },
  accrual_tracker: {
    description:
      "Track expected accruals, reversal timing, counterparties, and the ledger accounts they should reconcile to.",
    fields: [
      { key: "ref", label: "Reference", required: true, type: "text" },
      { key: "description", label: "Description", required: true, type: "text" },
      { key: "account_code", label: "Account code", required: true, type: "text" },
      { key: "amount", label: "Amount", required: true, type: "number" },
      { key: "period", label: "Period", required: true, type: "month" },
      { key: "reversal_date", label: "Reversal date", type: "date" },
      { key: "counterparty", label: "Counterparty", type: "text" },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    tableColumns: [
      { key: "ref", label: "Reference" },
      { key: "description", label: "Description" },
      { key: "account_code", label: "Account" },
      { key: "period", label: "Period" },
      { key: "amount", label: "Amount" },
      { key: "counterparty", label: "Counterparty" },
    ],
  },
  budget_vs_actual: {
    description:
      "Maintain the budget workpaper lines and optional dimensional ownership used for variance analysis in reports.",
    fields: [
      { key: "account_code", label: "Account code", required: true, type: "text" },
      { key: "period", label: "Period", required: true, type: "month" },
      { key: "budget_amount", label: "Budget amount", required: true, type: "number" },
      { key: "department", label: "Department", type: "text" },
      { key: "cost_centre", label: "Cost centre", type: "text" },
      { key: "project", label: "Project", type: "text" },
      { key: "notes", label: "Notes", type: "textarea" },
    ],
    tableColumns: [
      { key: "account_code", label: "Account" },
      { key: "period", label: "Period" },
      { key: "budget_amount", label: "Budget" },
      { key: "department", label: "Department" },
      { key: "cost_centre", label: "Cost centre" },
      { key: "project", label: "Project" },
    ],
  },
};

export default function CloseRunSchedulesPage(): ReactElement {
  const routeParams = useParams<{ closeRunId: string; entityId: string }>();
  const closeRunId = requireRouteParam(routeParams.closeRunId, "closeRunId");
  const entityId = requireRouteParam(routeParams.entityId, "entityId");
  const [editingRowIds, setEditingRowIds] = useState<Record<string, string | null>>({});
  const [drafts, setDrafts] = useState<Record<string, DraftState>>({});
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [mutatingScheduleTypes, setMutatingScheduleTypes] = useState<Record<string, boolean>>({});
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [statusNotes, setStatusNotes] = useState<Record<string, string>>({});
  const [workspace, setWorkspace] = useState<ReadonlyArray<SupportingScheduleDetail>>([]);

  useEffect(() => {
    void loadWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: (nextWorkspace) => {
        setWorkspace(nextWorkspace.schedules);
        setStatusNotes((previous) => {
          const nextNotes = { ...previous };
          for (const schedule of nextWorkspace.schedules) {
            if (nextNotes[schedule.schedule.scheduleType] === undefined) {
              nextNotes[schedule.schedule.scheduleType] = schedule.schedule.note ?? "";
            }
          }
          return nextNotes;
        });
      },
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

  const summaryMetrics = useMemo(() => {
    const approved = workspace.filter((schedule) => schedule.schedule.status === "approved").length;
    const notApplicable = workspace.filter(
      (schedule) => schedule.schedule.status === "not_applicable",
    ).length;
    const totalRows = workspace.reduce((count, schedule) => count + schedule.rows.length, 0);
    return {
      approved,
      notApplicable,
      totalRows,
    };
  }, [workspace]);

  function replaceScheduleDetail(nextDetail: SupportingScheduleDetail): void {
    setWorkspace((previous) =>
      previous.map((detail) =>
        detail.schedule.scheduleType === nextDetail.schedule.scheduleType ? nextDetail : detail,
      ),
    );
    setStatusNotes((previous) => ({
      ...previous,
      [nextDetail.schedule.scheduleType]:
        nextDetail.schedule.note ?? previous[nextDetail.schedule.scheduleType] ?? "",
    }));
  }

  function beginDraft(
    scheduleType: SupportingScheduleType,
    row?: SupportingScheduleDetail["rows"][number],
  ): void {
    setEditingRowIds((previous) => ({
      ...previous,
      [scheduleType]: row?.id ?? null,
    }));
    setDrafts((previous) => ({
      ...previous,
      [scheduleType]: row ? buildDraftFromPayload(row.payload) : buildEmptyDraft(scheduleType),
    }));
    setStatusMessage(null);
    setErrorMessage(null);
  }

  function clearDraft(scheduleType: SupportingScheduleType): void {
    setEditingRowIds((previous) => {
      const next = { ...previous };
      delete next[scheduleType];
      return next;
    });
    setDrafts((previous) => ({
      ...previous,
      [scheduleType]: buildEmptyDraft(scheduleType),
    }));
  }

  function updateDraft(scheduleType: SupportingScheduleType, key: string, value: string): void {
    setDrafts((previous) => ({
      ...previous,
      [scheduleType]: {
        ...(previous[scheduleType] ?? buildEmptyDraft(scheduleType)),
        [key]: value,
      },
    }));
  }

  async function handleSaveRow(scheduleType: SupportingScheduleType): Promise<void> {
    const draft = drafts[scheduleType] ?? buildEmptyDraft(scheduleType);
    setMutatingScheduleTypes((previous) => ({
      ...previous,
      [scheduleType]: true,
    }));
    setErrorMessage(null);
    setStatusMessage(null);
    try {
      const rowId = editingRowIds[scheduleType];
      const detail = await saveSupportingScheduleRow({
        entityId,
        closeRunId,
        scheduleType,
        payload: buildPayload(scheduleType, draft),
        ...(typeof rowId === "string" ? { rowId } : {}),
      });
      replaceScheduleDetail(detail);
      clearDraft(scheduleType);
      setStatusMessage(`${detail.schedule.label} updated.`);
    } catch (error: unknown) {
      setErrorMessage(resolveScheduleErrorMessage(error));
    } finally {
      setMutatingScheduleTypes((previous) => ({
        ...previous,
        [scheduleType]: false,
      }));
    }
  }

  async function handleDeleteRow(
    scheduleType: SupportingScheduleType,
    rowId: string,
  ): Promise<void> {
    setMutatingScheduleTypes((previous) => ({
      ...previous,
      [scheduleType]: true,
    }));
    setErrorMessage(null);
    setStatusMessage(null);
    try {
      const detail = await deleteSupportingScheduleRow({
        entityId,
        closeRunId,
        scheduleType,
        rowId,
      });
      replaceScheduleDetail(detail);
      if (editingRowIds[scheduleType] === rowId) {
        clearDraft(scheduleType);
      }
      setStatusMessage(`${detail.schedule.label} row deleted.`);
    } catch (error: unknown) {
      setErrorMessage(resolveScheduleErrorMessage(error));
    } finally {
      setMutatingScheduleTypes((previous) => ({
        ...previous,
        [scheduleType]: false,
      }));
    }
  }

  async function handleUpdateStatus(
    scheduleType: SupportingScheduleType,
    status: Exclude<SupportingScheduleStatus, "draft">,
  ): Promise<void> {
    setMutatingScheduleTypes((previous) => ({
      ...previous,
      [scheduleType]: true,
    }));
    setErrorMessage(null);
    setStatusMessage(null);
    try {
      const detail = await updateSupportingScheduleStatus({
        entityId,
        closeRunId,
        scheduleType,
        status,
        note: statusNotes[scheduleType] ?? "",
      });
      replaceScheduleDetail(detail);
      setStatusMessage(`${detail.schedule.label} marked ${formatScheduleStatus(status)}.`);
    } catch (error: unknown) {
      setErrorMessage(resolveScheduleErrorMessage(error));
    } finally {
      setMutatingScheduleTypes((previous) => ({
        ...previous,
        [scheduleType]: false,
      }));
    }
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading supporting schedules...</div>
        </section>
      </div>
    );
  }

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <p className="quartz-kpi-label">Step 06 Workspace</p>
            <h1>Supporting Schedules</h1>
            <p className="quartz-page-subtitle">
              Maintain the workpapers that support Reconciliation and Reporting.
            </p>
          </div>

          <div className="quartz-page-toolbar">
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}`}
            >
              Close-run overview
            </Link>
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/reconciliation`}
            >
              Reconciliation
            </Link>
          </div>
        </header>

        {statusMessage ? (
          <div className="status-banner success quartz-section" role="status">
            {statusMessage}
          </div>
        ) : null}

        {errorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid">
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Schedules</p>
              <p className="quartz-kpi-value">{workspace.length}</p>
              <p className="quartz-kpi-meta">Fixed assets, loans, accruals, budget</p>
            </article>
            <article className="quartz-kpi-tile highlight">
              <p className="quartz-kpi-label">Approved</p>
              <p className="quartz-kpi-value">{summaryMetrics.approved}</p>
              <p className="quartz-kpi-meta">Ready for reporting controls</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Not Applicable</p>
              <p className="quartz-kpi-value">{summaryMetrics.notApplicable}</p>
              <p className="quartz-kpi-meta">Explicitly cleared with notes</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Rows</p>
              <p className="quartz-kpi-value">{summaryMetrics.totalRows}</p>
              <p className="quartz-kpi-meta">Total supporting workpaper lines</p>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Schedule Editors</h2>
            <span className="quartz-queue-meta">
              Approve each schedule or mark it not applicable before reporting.
            </span>
          </div>
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card">
              <p className="quartz-kpi-label">Fixed Asset Register</p>
              <p className="form-helper">
                Track cost, accumulated depreciation, NBV, and disposal timing.
              </p>
            </article>
            <article className="quartz-card">
              <p className="quartz-kpi-label">Loan Amortisation</p>
              <p className="form-helper">
                Track payment schedules, principal, interest, and outstanding balances.
              </p>
            </article>
            <article className="quartz-card">
              <p className="quartz-kpi-label">Accrual Tracker</p>
              <p className="form-helper">
                Track expected accruals, reversals, and the ledger accounts they reconcile to.
              </p>
            </article>
            <article className="quartz-card">
              <p className="quartz-kpi-label">Budget vs Actual</p>
              <p className="form-helper">
                Track budget workpaper lines and optional dimension ownership for variance
                reporting.
              </p>
            </article>
          </div>
        </section>

        <section className="quartz-section quartz-review-main-stack">
          {workspace.map((detail) => {
            const definition = SCHEDULE_DEFINITIONS[detail.schedule.scheduleType];
            const scheduleType = detail.schedule.scheduleType;
            const draft = drafts[scheduleType] ?? buildEmptyDraft(scheduleType);
            const isMutating = mutatingScheduleTypes[scheduleType] === true;
            const isEditing = editingRowIds[scheduleType] !== undefined;
            const noteDraft = statusNotes[scheduleType] ?? detail.schedule.note ?? "";

            return (
              <article className="quartz-card quartz-card-table-shell" key={scheduleType}>
                <div className="quartz-section-header">
                  <div>
                    <h2 className="quartz-section-title">{detail.schedule.label}</h2>
                    <p className="quartz-table-secondary">
                      {detail.schedule.rowCount} row(s) • last updated{" "}
                      {formatTimestamp(detail.schedule.updatedAt)}
                    </p>
                  </div>
                  <span className="quartz-status-badge warning">
                    {formatScheduleStatus(detail.schedule.status)}
                  </span>
                </div>

                <div className="quartz-card-form-area">
                  <p className="form-helper">{definition.description}</p>
                  <label>
                    <span className="quartz-kpi-label">Review note</span>
                    <textarea
                      className="text-input"
                      onChange={(event) => {
                        setStatusNotes((previous) => ({
                          ...previous,
                          [scheduleType]: event.target.value,
                        }));
                      }}
                      rows={3}
                      value={noteDraft}
                    />
                  </label>

                  <div className="quartz-inline-action-row">
                    <button
                      className="secondary-button"
                      disabled={isMutating}
                      onClick={() => {
                        void handleUpdateStatus(scheduleType, "in_review");
                      }}
                      type="button"
                    >
                      Mark in review
                    </button>
                    <button
                      className="primary-button"
                      disabled={isMutating}
                      onClick={() => {
                        void handleUpdateStatus(scheduleType, "approved");
                      }}
                      type="button"
                    >
                      Approve schedule
                    </button>
                    <button
                      className="secondary-button"
                      disabled={isMutating}
                      onClick={() => {
                        void handleUpdateStatus(scheduleType, "not_applicable");
                      }}
                      type="button"
                    >
                      Mark not applicable
                    </button>
                  </div>
                </div>

                <div className="quartz-table-shell">
                  <table className="quartz-table">
                    <thead>
                      <tr>
                        {definition.tableColumns.map((column) => (
                          <th key={column.key}>{column.label}</th>
                        ))}
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {detail.rows.length === 0 ? (
                        <tr>
                          <td colSpan={definition.tableColumns.length + 1}>
                            <div className="quartz-empty-state">No rows have been added yet.</div>
                          </td>
                        </tr>
                      ) : (
                        detail.rows.map((row) => (
                          <tr key={row.id}>
                            {definition.tableColumns.map((column) => (
                              <td key={`${row.id}:${column.key}`}>
                                {formatPayloadValue(row.payload[column.key])}
                              </td>
                            ))}
                            <td>
                              <div className="quartz-inline-action-row">
                                <button
                                  className="secondary-button"
                                  onClick={() => {
                                    beginDraft(scheduleType, row);
                                  }}
                                  type="button"
                                >
                                  Edit
                                </button>
                                <button
                                  className="secondary-button"
                                  disabled={isMutating}
                                  onClick={() => {
                                    void handleDeleteRow(scheduleType, row.id);
                                  }}
                                  type="button"
                                >
                                  Delete
                                </button>
                              </div>
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>

                <div className="quartz-card-form-area">
                  <div className="quartz-inline-action-row">
                    <button
                      className="primary-button"
                      disabled={isMutating}
                      onClick={() => {
                        beginDraft(scheduleType);
                      }}
                      type="button"
                    >
                      Add row
                    </button>
                  </div>

                  {isEditing ? (
                    <div className="quartz-card-form-area">
                      <div className="quartz-section-header quartz-section-header-tight">
                        <div>
                          <h3 className="quartz-section-title">
                            {editingRowIds[scheduleType] ? "Edit row" : "New row"}
                          </h3>
                          <p className="quartz-table-secondary">
                            Changes return the schedule to in-review status.
                          </p>
                        </div>
                      </div>

                      <div className="quartz-form-grid">
                        {definition.fields.map((field) => (
                          <label
                            className="quartz-form-label"
                            key={`${scheduleType}:${field.key}`}
                            style={field.type === "textarea" ? textareaFieldShellStyle : undefined}
                          >
                            <span>
                              {field.label}
                              {field.required ? " *" : ""}
                            </span>
                            {field.type === "textarea" ? (
                              <textarea
                                className="text-input"
                                onChange={(event) =>
                                  updateDraft(scheduleType, field.key, event.target.value)
                                }
                                placeholder={field.placeholder}
                                rows={3}
                                value={draft[field.key] ?? ""}
                              />
                            ) : (
                              <input
                                className="text-input"
                                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                                  updateDraft(scheduleType, field.key, event.target.value)
                                }
                                placeholder={field.placeholder}
                                step={field.type === "number" ? "0.01" : undefined}
                                type={field.type}
                                value={draft[field.key] ?? ""}
                              />
                            )}
                          </label>
                        ))}
                      </div>

                      <div className="quartz-inline-action-row">
                        <button
                          className="primary-button"
                          disabled={isMutating}
                          onClick={() => {
                            void handleSaveRow(scheduleType);
                          }}
                          type="button"
                        >
                          {isMutating ? "Saving..." : "Save row"}
                        </button>
                        <button
                          className="secondary-button"
                          disabled={isMutating}
                          onClick={() => {
                            clearDraft(scheduleType);
                          }}
                          type="button"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : null}
                </div>
              </article>
            );
          })}
        </section>
      </section>
    </div>
  );
}

async function loadWorkspace(options: {
  closeRunId: string;
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: Awaited<ReturnType<typeof readSupportingScheduleWorkspace>>) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  options.onError(null);
  try {
    const workspace = await readSupportingScheduleWorkspace(options.entityId, options.closeRunId);
    options.onLoaded(workspace);
  } catch (error: unknown) {
    options.onError(resolveScheduleErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function buildDraftFromPayload(payload: Record<string, unknown>): DraftState {
  return Object.fromEntries(
    Object.entries(payload).map(([key, value]) => [key, normalizeUnknownValue(value)]),
  );
}

function buildEmptyDraft(scheduleType: SupportingScheduleType): DraftState {
  return Object.fromEntries(
    SCHEDULE_DEFINITIONS[scheduleType].fields.map((field) => [field.key, ""]),
  );
}

function buildPayload(
  scheduleType: SupportingScheduleType,
  draft: DraftState,
): SupportingScheduleRowPayload {
  const cleanedEntries = Object.entries(draft)
    .map(([key, value]): [string, string] => [key, (value ?? "").trim()])
    .filter(([, value]) => value.length > 0);
  const cleaned: DraftState = Object.fromEntries(cleanedEntries);

  if (scheduleType === "fixed_assets") {
    return cleaned as SupportingScheduleRowPayload;
  }
  if (scheduleType === "loan_amortisation") {
    return {
      ...cleaned,
      payment_no: Number(cleaned.payment_no ?? 0),
    } as SupportingScheduleRowPayload;
  }
  return cleaned as SupportingScheduleRowPayload;
}

function formatScheduleStatus(status: SupportingScheduleStatus): string {
  return status.replaceAll("_", " ");
}

function formatPayloadValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string" && value.trim().length === 0) return "—";
  return normalizeUnknownValue(value);
}

function normalizeUnknownValue(value: unknown): string {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.map((item) => normalizeUnknownValue(item)).join(", ");
  }
  return JSON.stringify(value);
}

function formatTimestamp(value: string | null): string {
  if (!value) return "just now";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

function resolveScheduleErrorMessage(error: unknown): string {
  if (error instanceof SupportingScheduleApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return "The supporting schedule request failed.";
}

const textareaFieldShellStyle: Readonly<Record<string, string | number>> = {
  gridColumn: "1 / -1",
};
