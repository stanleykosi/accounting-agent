/*
Purpose: Render the entity imported-ledger workspace with Quartz-aligned GL/TB upload controls.
Scope: Client-side ledger workspace reads plus baseline upload actions through same-origin APIs.
Dependencies: React hooks, route params, Next links, and the ledger API helper module.
*/

"use client";

import Link from "next/link";
import {
  use,
  useEffect,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../../../../../components/layout/QuartzIcons";
import {
  LedgerApiError,
  readLedgerWorkspace,
  uploadGeneralLedger,
  uploadTrialBalance,
  type LedgerWorkspaceResponse,
} from "../../../../../lib/ledger";

type LedgerPageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

type UploadFormState = {
  periodEnd: string;
  periodStart: string;
};

const defaultUploadFormState: UploadFormState = {
  periodEnd: "",
  periodStart: "",
};

export default function EntityLedgerPage({ params }: Readonly<LedgerPageProps>): ReactElement {
  const { entityId } = use(params);
  const [workspace, setWorkspace] = useState<LedgerWorkspaceResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();
  const [glFile, setGlFile] = useState<File | null>(null);
  const [tbFile, setTbFile] = useState<File | null>(null);
  const [glForm, setGlForm] = useState<UploadFormState>(defaultUploadFormState);
  const [tbForm, setTbForm] = useState<UploadFormState>(defaultUploadFormState);

  useEffect(() => {
    void loadWorkspace({
      entityId,
      onError: setErrorMessage,
      onLoaded: setWorkspace,
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

  const handleFileChange =
    (kind: "gl" | "tb") =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      const file = event.target.files?.[0] ?? null;
      if (kind === "gl") {
        setGlFile(file);
      } else {
        setTbFile(file);
      }
      setStatusMessage(null);
      setErrorMessage(null);
    };

  const handlePeriodChange =
    (kind: "gl" | "tb", field: keyof UploadFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      const nextValue = event.target.value;
      if (kind === "gl") {
        setGlForm((current) => ({ ...current, [field]: nextValue }));
      } else {
        setTbForm((current) => ({ ...current, [field]: nextValue }));
      }
    };

  const handleGlUpload = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (glFile === null) {
      setErrorMessage("Select a GL CSV or XLSX file to upload.");
      return;
    }
    if (!glForm.periodStart || !glForm.periodEnd) {
      setErrorMessage("Select the imported GL period start and end dates.");
      return;
    }

    startTransition(() => {
      void uploadGeneralLedger(entityId, {
        file: glFile,
        periodStart: glForm.periodStart,
        periodEnd: glForm.periodEnd,
      })
        .then((response) => {
          setWorkspace(response.workspace);
          setGlFile(null);
          setGlForm(defaultUploadFormState);
          setErrorMessage(null);
          setStatusMessage(buildUploadStatusMessage(response.auto_bound_close_run_ids, response.skipped_close_run_ids));
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveLedgerErrorMessage(error));
        });
    });
  };

  const handleTbUpload = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (tbFile === null) {
      setErrorMessage("Select a trial balance CSV or XLSX file to upload.");
      return;
    }
    if (!tbForm.periodStart || !tbForm.periodEnd) {
      setErrorMessage("Select the imported trial balance period start and end dates.");
      return;
    }

    startTransition(() => {
      void uploadTrialBalance(entityId, {
        file: tbFile,
        periodStart: tbForm.periodStart,
        periodEnd: tbForm.periodEnd,
      })
        .then((response) => {
          setWorkspace(response.workspace);
          setTbFile(null);
          setTbForm(defaultUploadFormState);
          setErrorMessage(null);
          setStatusMessage(buildUploadStatusMessage(response.auto_bound_close_run_ids, response.skipped_close_run_ids));
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveLedgerErrorMessage(error));
        });
    });
  };

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading imported ledger workspace...</div>
        </section>
      </div>
    );
  }

  if (workspace === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The imported-ledger workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Imported Ledger</h1>
            <p className="quartz-page-subtitle">
              Upload entity-level general ledger and trial balance baselines. Matching close runs
              can bind to these imports and reconcile against them while preserving audit-ready
              lineage.
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <Link className="secondary-button quartz-toolbar-button" href={`/entities/${entityId}/settings`}>
              <QuartzIcon className="quartz-inline-icon" name="settings" />
              Workspace Settings
            </Link>
            <Link className="secondary-button quartz-toolbar-button" href={`/entities/${entityId}`}>
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Entity Home
            </Link>
          </div>
        </header>

        {statusMessage ? (
          <div className="status-banner success quartz-section" role="status">
            {statusMessage}
          </div>
        ) : null}
        {errorMessage ? (
          <div className="status-banner danger quartz-section" role="alert">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid">
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">GL Imports</p>
              <p className="quartz-kpi-value">{workspace.general_ledger_imports.length}</p>
              <p className="quartz-kpi-meta">Uploaded general ledger baselines</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">TB Imports</p>
              <p className="quartz-kpi-value">{workspace.trial_balance_imports.length}</p>
              <p className="quartz-kpi-meta">Uploaded trial balance baselines</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Bound Close Runs</p>
              <p className="quartz-kpi-value">{workspace.close_run_bindings.length}</p>
              <p className="quartz-kpi-meta">Runs currently using imported baselines</p>
            </article>
            <article className="quartz-kpi-tile highlight">
              <p className="quartz-kpi-label">Canonical Mode</p>
              <p className="quartz-kpi-value">Entity level</p>
              <p className="quartz-kpi-meta">Imports are governed before close-run binding</p>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">General Ledger</p>
                  <h2 className="quartz-section-title">Upload GL Baseline</h2>
                </div>
              </div>
              <form className="quartz-settings-form" onSubmit={handleGlUpload}>
                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Period Start</span>
                    <input
                      className="text-input"
                      onChange={handlePeriodChange("gl", "periodStart")}
                      type="date"
                      value={glForm.periodStart}
                    />
                  </label>
                  <label className="quartz-form-label">
                    <span>Period End</span>
                    <input
                      className="text-input"
                      onChange={handlePeriodChange("gl", "periodEnd")}
                      type="date"
                      value={glForm.periodEnd}
                    />
                  </label>
                </div>
                <label className="quartz-form-label">
                  <span>GL File</span>
                  <input
                    accept=".csv,.xlsx,.xlsm"
                    className="text-input"
                    onChange={handleFileChange("gl")}
                    type="file"
                  />
                </label>
                <div className="quartz-button-row">
                  <button className="primary-button" disabled={isPending} type="submit">
                    {isPending ? "Uploading..." : "Upload GL Baseline"}
                  </button>
                </div>
              </form>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Trial Balance</p>
                  <h2 className="quartz-section-title">Upload TB Baseline</h2>
                </div>
              </div>
              <form className="quartz-settings-form" onSubmit={handleTbUpload}>
                <div className="quartz-form-grid">
                  <label className="quartz-form-label">
                    <span>Period Start</span>
                    <input
                      className="text-input"
                      onChange={handlePeriodChange("tb", "periodStart")}
                      type="date"
                      value={tbForm.periodStart}
                    />
                  </label>
                  <label className="quartz-form-label">
                    <span>Period End</span>
                    <input
                      className="text-input"
                      onChange={handlePeriodChange("tb", "periodEnd")}
                      type="date"
                      value={tbForm.periodEnd}
                    />
                  </label>
                </div>
                <label className="quartz-form-label">
                  <span>TB File</span>
                  <input
                    accept=".csv,.xlsx,.xlsm"
                    className="text-input"
                    onChange={handleFileChange("tb")}
                    type="file"
                  />
                </label>
                <div className="quartz-button-row">
                  <button className="primary-button" disabled={isPending} type="submit">
                    {isPending ? "Uploading..." : "Upload Trial Balance"}
                  </button>
                </div>
              </form>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">History</p>
                  <h2 className="quartz-section-title">General Ledger Imports</h2>
                </div>
              </div>
              {workspace.general_ledger_imports.length === 0 ? (
                <div className="quartz-empty-state quartz-empty-state-compact">
                  No general ledger baselines uploaded yet.
                </div>
              ) : (
                <div className="quartz-summary-list">
                  {workspace.general_ledger_imports.map((item) => (
                    <div className="quartz-summary-row" key={item.id}>
                      <div>
                        <strong>{item.uploaded_filename}</strong>
                        <div className="quartz-table-secondary">
                          {item.period_start} to {item.period_end}
                        </div>
                      </div>
                      <strong>
                        {item.row_count} rows • {item.source_format.toUpperCase()}
                      </strong>
                    </div>
                  ))}
                </div>
              )}
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">History</p>
                  <h2 className="quartz-section-title">Trial Balance Imports</h2>
                </div>
              </div>
              {workspace.trial_balance_imports.length === 0 ? (
                <div className="quartz-empty-state quartz-empty-state-compact">
                  No trial balance baselines uploaded yet.
                </div>
              ) : (
                <div className="quartz-summary-list">
                  {workspace.trial_balance_imports.map((item) => (
                    <div className="quartz-summary-row" key={item.id}>
                      <div>
                        <strong>{item.uploaded_filename}</strong>
                        <div className="quartz-table-secondary">
                          {item.period_start} to {item.period_end}
                        </div>
                      </div>
                      <strong>
                        {item.row_count} rows • {item.source_format.toUpperCase()}
                      </strong>
                    </div>
                  ))}
                </div>
              )}
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <article className="quartz-card quartz-card-table-shell">
            <div className="quartz-section-header">
              <div>
                <h2 className="quartz-section-title">Close-Run Bindings</h2>
                <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                  Current imported baselines in active operational use.
                </p>
              </div>
            </div>
            {workspace.close_run_bindings.length === 0 ? (
              <div className="quartz-empty-state">
                No close runs are currently bound to imported GL or TB baselines.
              </div>
            ) : (
              <table className="quartz-table">
                <thead>
                  <tr>
                    <th>Close Run</th>
                    <th>Binding Source</th>
                    <th>GL Batch</th>
                    <th>TB Batch</th>
                  </tr>
                </thead>
                <tbody>
                  {workspace.close_run_bindings.map((binding) => (
                    <tr key={binding.close_run_id}>
                      <td>
                        <div className="quartz-table-primary">
                          {binding.close_run_id.slice(0, 8)}
                        </div>
                      </td>
                      <td>
                        <div className="quartz-table-primary">{binding.binding_source}</div>
                      </td>
                      <td>
                        <div className="quartz-table-secondary">
                          {binding.general_ledger_import_batch_id?.slice(0, 8) ?? "None"}
                        </div>
                      </td>
                      <td>
                        <div className="quartz-table-secondary">
                          {binding.trial_balance_import_batch_id?.slice(0, 8) ?? "None"}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </article>
        </section>
      </section>
    </div>
  );
}

async function loadWorkspace(options: {
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: LedgerWorkspaceResponse) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const workspace = await readLedgerWorkspace(options.entityId);
    options.onLoaded(workspace);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveLedgerErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function buildUploadStatusMessage(
  autoBoundCloseRunIds: readonly string[],
  skippedCloseRunIds: readonly string[],
): string {
  if (autoBoundCloseRunIds.length === 0 && skippedCloseRunIds.length === 0) {
    return "Import uploaded successfully.";
  }
  if (skippedCloseRunIds.length === 0) {
    return `Import uploaded and auto-bound to ${autoBoundCloseRunIds.length} close run(s).`;
  }
  if (autoBoundCloseRunIds.length === 0) {
    return (
      `Import uploaded, but ${skippedCloseRunIds.length} matching close run(s) were left unbound `
      + "because they already have ledger activity."
    );
  }
  return (
    `Import uploaded, auto-bound to ${autoBoundCloseRunIds.length} close run(s), and skipped `
    + `${skippedCloseRunIds.length} started close run(s).`
  );
}

function resolveLedgerErrorMessage(error: unknown): string {
  if (error instanceof LedgerApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "The imported-ledger request failed. Reload the page and try again.";
}
