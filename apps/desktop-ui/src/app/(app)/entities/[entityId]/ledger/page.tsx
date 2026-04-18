/*
Purpose: Render the entity imported-ledger workspace with GL/TB upload controls.
Scope: Client-side ledger workspace reads plus baseline upload actions through same-origin APIs.
Dependencies: React hooks, route params, shared SurfaceCard, and the ledger API helper module.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
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
      <div className="app-shell coa-page">
        <SurfaceCard title="Loading Imported Ledger" subtitle="Entity ledger baselines">
          <p className="form-helper">Loading imported GL/TB baselines and close-run bindings...</p>
        </SurfaceCard>
      </div>
    );
  }

  if (workspace === null) {
    return (
      <div className="app-shell coa-page">
        <SurfaceCard title="Ledger Workspace Unavailable" subtitle="Entity ledger baselines">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The imported-ledger workspace could not be loaded."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  return (
    <div className="app-shell coa-page">
      <section className="hero-grid entity-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Imported Ledger</p>
          <h1>Ledger Baselines</h1>
          <p className="lede">
            Upload an entity-level general ledger or trial balance baseline for a period. Close
            runs still work without these imports, but matching-period runs can bind to them and
            reconcile against the imported baseline plus current-run journals.
          </p>
          <div className="coa-hero-actions">
            <Link className="secondary-button" href={`/entities/${entityId}`}>
              Back to workspace
            </Link>
          </div>
        </div>

        <SurfaceCard title="Current Baseline State" subtitle="At a glance" tone="accent">
          <dl className="entity-meta-grid workspace-snapshot-grid">
            <div>
              <dt>GL imports</dt>
              <dd>{workspace.general_ledger_imports.length}</dd>
            </div>
            <div>
              <dt>TB imports</dt>
              <dd>{workspace.trial_balance_imports.length}</dd>
            </div>
            <div>
              <dt>Bound close runs</dt>
              <dd>{workspace.close_run_bindings.length}</dd>
            </div>
            <div>
              <dt>Runs without imports</dt>
              <dd>Allowed</dd>
            </div>
          </dl>
        </SurfaceCard>
      </section>

      {statusMessage ? (
        <div className="status-banner success" role="status">
          {statusMessage}
        </div>
      ) : null}
      {errorMessage ? (
        <div className="status-banner danger" role="alert">
          {errorMessage}
        </div>
      ) : null}

      <section className="coa-grid">
        <SurfaceCard title="Upload General Ledger" subtitle="CSV or XLSX baseline">
          <form className="coa-upload-form" onSubmit={handleGlUpload}>
            <label className="field">
              <span>Period start</span>
              <input onChange={handlePeriodChange("gl", "periodStart")} type="date" value={glForm.periodStart} />
            </label>
            <label className="field">
              <span>Period end</span>
              <input onChange={handlePeriodChange("gl", "periodEnd")} type="date" value={glForm.periodEnd} />
            </label>
            <label className="field">
              <span>GL file</span>
              <input accept=".csv,.xlsx,.xlsm" onChange={handleFileChange("gl")} type="file" />
            </label>
            <button className="primary-button" disabled={isPending} type="submit">
              {isPending ? "Uploading..." : "Upload GL baseline"}
            </button>
          </form>
        </SurfaceCard>

        <SurfaceCard title="Upload Trial Balance" subtitle="CSV or XLSX baseline">
          <form className="coa-upload-form" onSubmit={handleTbUpload}>
            <label className="field">
              <span>Period start</span>
              <input onChange={handlePeriodChange("tb", "periodStart")} type="date" value={tbForm.periodStart} />
            </label>
            <label className="field">
              <span>Period end</span>
              <input onChange={handlePeriodChange("tb", "periodEnd")} type="date" value={tbForm.periodEnd} />
            </label>
            <label className="field">
              <span>TB file</span>
              <input accept=".csv,.xlsx,.xlsm" onChange={handleFileChange("tb")} type="file" />
            </label>
            <button className="primary-button" disabled={isPending} type="submit">
              {isPending ? "Uploading..." : "Upload trial balance"}
            </button>
          </form>
        </SurfaceCard>
      </section>

      <section className="coa-grid">
        <SurfaceCard title="General Ledger Imports" subtitle="Newest first">
          {workspace.general_ledger_imports.length === 0 ? (
            <p className="form-helper">No GL baselines uploaded yet.</p>
          ) : (
            <div className="coa-set-list">
              {workspace.general_ledger_imports.map((item) => (
                <article className="coa-set-card" key={item.id}>
                  <h3>{item.uploaded_filename}</h3>
                  <p>{item.period_start} to {item.period_end}</p>
                  <p>{item.row_count} row(s) • {item.source_format.toUpperCase()}</p>
                </article>
              ))}
            </div>
          )}
        </SurfaceCard>

        <SurfaceCard title="Trial Balance Imports" subtitle="Newest first">
          {workspace.trial_balance_imports.length === 0 ? (
            <p className="form-helper">No trial balance baselines uploaded yet.</p>
          ) : (
            <div className="coa-set-list">
              {workspace.trial_balance_imports.map((item) => (
                <article className="coa-set-card" key={item.id}>
                  <h3>{item.uploaded_filename}</h3>
                  <p>{item.period_start} to {item.period_end}</p>
                  <p>{item.row_count} row(s) • {item.source_format.toUpperCase()}</p>
                </article>
              ))}
            </div>
          )}
        </SurfaceCard>
      </section>

      <SurfaceCard title="Close-Run Bindings" subtitle="Current imported baselines in use">
        {workspace.close_run_bindings.length === 0 ? (
          <p className="form-helper">No close runs are currently bound to imported GL/TB baselines.</p>
        ) : (
          <div className="coa-set-list">
            {workspace.close_run_bindings.map((binding) => (
              <article className="coa-set-card" key={binding.close_run_id}>
                <h3>Close run {binding.close_run_id.slice(0, 8)}</h3>
                <p>Binding source: {binding.binding_source}</p>
                <p>GL: {binding.general_ledger_import_batch_id ? binding.general_ledger_import_batch_id.slice(0, 8) : "none"}</p>
                <p>TB: {binding.trial_balance_import_batch_id ? binding.trial_balance_import_batch_id.slice(0, 8) : "none"}</p>
              </article>
            ))}
          </div>
        )}
      </SurfaceCard>
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
