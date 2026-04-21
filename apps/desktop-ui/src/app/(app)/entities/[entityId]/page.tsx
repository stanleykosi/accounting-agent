/*
Purpose: Render the operational entity home with live close-run context and start-close controls.
Scope: Client-side entity and close-run reads, plus governed close-run creation through the same-origin API.
Dependencies: React hooks, shared workflow metadata, Next.js routing, and entity/close-run helpers.
*/

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  useCallback,
  use,
  useEffect,
  useMemo,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../../../../components/layout/QuartzIcons";
import {
  CloseRunApiError,
  createCloseRun,
  deriveCloseRunAttention,
  findActivePhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  listCloseRuns,
  readCloseRunListSnapshot,
  type CloseRunSummary,
} from "../../../../lib/close-runs";
import {
  CoaApiError,
  readCoaWorkspace,
  uploadManualCoa,
  type CoaWorkspaceResponse,
} from "../../../../lib/coa";
import {
  EntityApiError,
  readEntityWorkspaceSnapshot,
  readEntityWorkspace,
  type EntityWorkspace,
} from "../../../../lib/entities/api";
import {
  LedgerApiError,
  readLedgerWorkspace,
  uploadGeneralLedger,
  uploadTrialBalance,
  type LedgerImportUploadResponse,
  type LedgerWorkspaceResponse,
} from "../../../../lib/ledger";

type EntityWorkspacePageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

type CreateCloseRunFormState = {
  periodEnd: string;
  periodStart: string;
};

type UploadEntityDataFormState = {
  periodEnd: string;
  periodStart: string;
};

type UploadEntityDataKind = "coa" | "general_ledger" | "trial_balance";

type MetricTile = {
  label: string;
  meta: string;
  tone?: "error" | "success" | undefined;
  value: string;
};

type UploadDatasetCard = {
  key: UploadEntityDataKind;
  label: string;
  meta: string;
  value: string;
};

const defaultCreateCloseRunFormState: CreateCloseRunFormState = {
  periodEnd: "",
  periodStart: "",
};

const defaultUploadEntityDataFormState: UploadEntityDataFormState = {
  periodEnd: "",
  periodStart: "",
};

export default function EntityWorkspacePage({
  params,
}: Readonly<EntityWorkspacePageProps>): ReactElement {
  const { entityId } = use(params);
  const entitySnapshot = readEntityWorkspaceSnapshot(entityId);
  const closeRunSnapshot = readCloseRunListSnapshot(entityId);
  const router = useRouter();
  const [entity, setEntity] = useState<EntityWorkspace | null>(entitySnapshot);
  const [entityErrorMessage, setEntityErrorMessage] = useState<string | null>(null);
  const [closeRunErrorMessage, setCloseRunErrorMessage] = useState<string | null>(null);
  const [closeRuns, setCloseRuns] = useState<readonly CloseRunSummary[]>(closeRunSnapshot ?? []);
  const [coaFile, setCoaFile] = useState<File | null>(null);
  const [coaWorkspace, setCoaWorkspace] = useState<CoaWorkspaceResponse | null>(null);
  const [isLoading, setIsLoading] = useState(
    () => entitySnapshot === null || closeRunSnapshot === null,
  );
  const [isPending, startTransition] = useTransition();
  const [isCreateCloseRunDialogOpen, setIsCreateCloseRunDialogOpen] = useState(false);
  const [glFile, setGlFile] = useState<File | null>(null);
  const [glUploadFormState, setGlUploadFormState] = useState<UploadEntityDataFormState>(
    defaultUploadEntityDataFormState,
  );
  const [isUploadDialogOpen, setIsUploadDialogOpen] = useState(false);
  const [isUploadWorkspaceLoading, setIsUploadWorkspaceLoading] = useState(false);
  const [isUploadPending, startUploadTransition] = useTransition();
  const [ledgerWorkspace, setLedgerWorkspace] = useState<LedgerWorkspaceResponse | null>(null);
  const [selectedUploadKind, setSelectedUploadKind] = useState<UploadEntityDataKind>("coa");
  const [tbFile, setTbFile] = useState<File | null>(null);
  const [tbUploadFormState, setTbUploadFormState] = useState<UploadEntityDataFormState>(
    defaultUploadEntityDataFormState,
  );
  const [uploadDialogErrorMessage, setUploadDialogErrorMessage] = useState<string | null>(null);
  const [uploadStatusMessage, setUploadStatusMessage] = useState<string | null>(null);
  const [closeRunFormState, setCloseRunFormState] = useState<CreateCloseRunFormState>(
    defaultCreateCloseRunFormState,
  );

  const refreshEntityHome = useCallback(async (): Promise<void> => {
    await loadWorkspaceView({
      entityId,
      onCloseRunsLoaded: setCloseRuns,
      onCloseRunError: setCloseRunErrorMessage,
      onError: setEntityErrorMessage,
      onLoaded: setEntity,
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

  useEffect(() => {
    void refreshEntityHome();
  }, [refreshEntityHome]);

  const activeCloseRun = useMemo(() => findWorkingCloseRun(closeRuns), [closeRuns]);
  const metricTiles = useMemo(
    () => buildEntityMetricTiles(entity, closeRuns),
    [closeRuns, entity],
  );

  const latestGeneralLedgerImport = ledgerWorkspace?.general_ledger_imports[0] ?? null;
  const latestTrialBalanceImport = ledgerWorkspace?.trial_balance_imports[0] ?? null;
  const activeCoaSet = coaWorkspace?.active_set ?? null;
  const uploadDatasetCards = useMemo(
    () =>
      buildUploadDatasetCards({
        activeCoaSet,
        latestGeneralLedgerImport,
        latestTrialBalanceImport,
      }),
    [activeCoaSet, latestGeneralLedgerImport, latestTrialBalanceImport],
  );

  const handleCloseRunFieldChange =
    (fieldName: keyof CreateCloseRunFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      setCloseRunFormState((currentState) => ({
        ...currentState,
        [fieldName]: event.target.value,
      }));
    };

  const handleCreateCloseRun = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setCloseRunErrorMessage(null);

    startTransition(() => {
      void createCloseRun(entityId, {
        period_end: closeRunFormState.periodEnd,
        period_start: closeRunFormState.periodStart,
      })
        .then((createdCloseRun) => {
          setCloseRuns((currentCloseRuns) => [createdCloseRun, ...currentCloseRuns]);
          setCloseRunFormState(defaultCreateCloseRunFormState);
          setIsCreateCloseRunDialogOpen(false);
          router.push(`/entities/${entityId}/close-runs/${createdCloseRun.id}`);
          router.refresh();
        })
        .catch((error: unknown) => {
          setCloseRunErrorMessage(resolveWorkspaceViewErrorMessage(error));
        });
    });
  };

  const handleUploadPeriodFieldChange =
    (kind: "gl" | "tb", fieldName: keyof UploadEntityDataFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      const nextValue = event.target.value;
      if (kind === "gl") {
        setGlUploadFormState((currentState) => ({
          ...currentState,
          [fieldName]: nextValue,
        }));
        return;
      }

      setTbUploadFormState((currentState) => ({
        ...currentState,
        [fieldName]: nextValue,
      }));
    };

  const handleUploadFileChange =
    (kind: UploadEntityDataKind) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      const nextFile = event.target.files?.[0] ?? null;
      setUploadDialogErrorMessage(null);
      setUploadStatusMessage(null);

      if (kind === "coa") {
        setCoaFile(nextFile);
        return;
      }

      if (kind === "general_ledger") {
        setGlFile(nextFile);
        return;
      }

      setTbFile(nextFile);
    };

  useEffect(() => {
    if (!isCreateCloseRunDialogOpen) {
      return;
    }

    function handleEscape(event: KeyboardEvent): void {
      if (event.key === "Escape" && !isPending) {
        setIsCreateCloseRunDialogOpen(false);
      }
    }

    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("keydown", handleEscape);
    };
  }, [isCreateCloseRunDialogOpen, isPending]);

  useEffect(() => {
    if (!isUploadDialogOpen) {
      return;
    }

    function handleEscape(event: KeyboardEvent): void {
      if (event.key === "Escape" && !isUploadPending) {
        setIsUploadDialogOpen(false);
      }
    }

    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("keydown", handleEscape);
    };
  }, [isUploadDialogOpen, isUploadPending]);

  function openCreateCloseRunDialog(): void {
    setCloseRunErrorMessage(null);
    setCloseRunFormState(defaultCreateCloseRunFormState);
    setIsCreateCloseRunDialogOpen(true);
  }

  function closeCreateCloseRunDialog(): void {
    if (isPending) {
      return;
    }

    setIsCreateCloseRunDialogOpen(false);
    setCloseRunErrorMessage(null);
  }

  async function loadUploadWorkspace(preferredKind?: UploadEntityDataKind): Promise<void> {
    setIsUploadWorkspaceLoading(true);

    const [coaResult, ledgerResult] = await Promise.allSettled([
      readCoaWorkspace(entityId),
      readLedgerWorkspace(entityId),
    ]);

    if (coaResult.status === "fulfilled") {
      setCoaWorkspace(coaResult.value);
    }

    if (ledgerResult.status === "fulfilled") {
      setLedgerWorkspace(ledgerResult.value);
    }

    const nextCoaWorkspace = coaResult.status === "fulfilled" ? coaResult.value : coaWorkspace;
    const nextLedgerWorkspace =
      ledgerResult.status === "fulfilled" ? ledgerResult.value : ledgerWorkspace;
    setSelectedUploadKind(
      preferredKind ?? resolvePreferredUploadKind(nextCoaWorkspace, nextLedgerWorkspace),
    );

    if (coaResult.status === "rejected" && ledgerResult.status === "rejected") {
      setUploadDialogErrorMessage(resolveEntityDataUploadError(coaResult.reason));
    } else if (coaResult.status === "rejected") {
      setUploadDialogErrorMessage(resolveEntityDataUploadError(coaResult.reason));
    } else if (ledgerResult.status === "rejected") {
      setUploadDialogErrorMessage(resolveEntityDataUploadError(ledgerResult.reason));
    } else {
      setUploadDialogErrorMessage(null);
    }

    setIsUploadWorkspaceLoading(false);
  }

  function openUploadDialog(kind?: UploadEntityDataKind): void {
    setSelectedUploadKind(
      kind ?? resolvePreferredUploadKind(coaWorkspace, ledgerWorkspace),
    );
    setCoaFile(null);
    setGlFile(null);
    setTbFile(null);
    setGlUploadFormState(
      activeCloseRun
        ? { periodEnd: activeCloseRun.periodEnd, periodStart: activeCloseRun.periodStart }
        : defaultUploadEntityDataFormState,
    );
    setTbUploadFormState(
      activeCloseRun
        ? { periodEnd: activeCloseRun.periodEnd, periodStart: activeCloseRun.periodStart }
        : defaultUploadEntityDataFormState,
    );
    setUploadDialogErrorMessage(null);
    setUploadStatusMessage(null);
    setIsUploadDialogOpen(true);
    void loadUploadWorkspace(kind);
  }

  function closeUploadDialog(): void {
    if (isUploadPending) {
      return;
    }

    setIsUploadDialogOpen(false);
    setUploadDialogErrorMessage(null);
    setUploadStatusMessage(null);
  }

  function handleUploadCoa(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (coaFile === null) {
      setUploadDialogErrorMessage("Select a CSV or XLSX file to upload.");
      return;
    }

    startUploadTransition(() => {
      void uploadManualCoa(entityId, coaFile)
        .then(async (nextWorkspace) => {
          setCoaWorkspace(nextWorkspace);
          setCoaFile(null);
          setUploadDialogErrorMessage(null);
          setUploadStatusMessage("Chart of accounts uploaded successfully.");
          setSelectedUploadKind(resolvePreferredUploadKind(nextWorkspace, ledgerWorkspace));
          await refreshEntityHome();
        })
        .catch((error: unknown) => {
          setUploadDialogErrorMessage(resolveEntityDataUploadError(error));
        });
    });
  }

  function handleUploadGeneralLedger(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (glFile === null) {
      setUploadDialogErrorMessage("Select a GL CSV or XLSX file to upload.");
      return;
    }
    if (!glUploadFormState.periodStart || !glUploadFormState.periodEnd) {
      setUploadDialogErrorMessage("Select the imported GL period start and end dates.");
      return;
    }

    startUploadTransition(() => {
      void uploadGeneralLedger(entityId, {
        file: glFile,
        periodEnd: glUploadFormState.periodEnd,
        periodStart: glUploadFormState.periodStart,
      })
        .then(async (response) => {
          setLedgerWorkspace(response.workspace);
          setGlFile(null);
          setGlUploadFormState(
            activeCloseRun
              ? { periodEnd: activeCloseRun.periodEnd, periodStart: activeCloseRun.periodStart }
              : defaultUploadEntityDataFormState,
          );
          setUploadDialogErrorMessage(null);
          setUploadStatusMessage(buildLedgerUploadStatusMessage("General ledger", response));
          setSelectedUploadKind(resolvePreferredUploadKind(coaWorkspace, response.workspace));
          await refreshEntityHome();
        })
        .catch((error: unknown) => {
          setUploadDialogErrorMessage(resolveEntityDataUploadError(error));
        });
    });
  }

  function handleUploadTrialBalance(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (tbFile === null) {
      setUploadDialogErrorMessage("Select a trial balance CSV or XLSX file to upload.");
      return;
    }
    if (!tbUploadFormState.periodStart || !tbUploadFormState.periodEnd) {
      setUploadDialogErrorMessage(
        "Select the imported trial balance period start and end dates.",
      );
      return;
    }

    startUploadTransition(() => {
      void uploadTrialBalance(entityId, {
        file: tbFile,
        periodEnd: tbUploadFormState.periodEnd,
        periodStart: tbUploadFormState.periodStart,
      })
        .then(async (response) => {
          setLedgerWorkspace(response.workspace);
          setTbFile(null);
          setTbUploadFormState(
            activeCloseRun
              ? { periodEnd: activeCloseRun.periodEnd, periodStart: activeCloseRun.periodStart }
              : defaultUploadEntityDataFormState,
          );
          setUploadDialogErrorMessage(null);
          setUploadStatusMessage(buildLedgerUploadStatusMessage("Trial balance", response));
          setSelectedUploadKind(resolvePreferredUploadKind(coaWorkspace, response.workspace));
          await refreshEntityHome();
        })
        .catch((error: unknown) => {
          setUploadDialogErrorMessage(resolveEntityDataUploadError(error));
        });
    });
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading entity home...</div>
        </section>
      </div>
    );
  }

  if (entity === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {entityErrorMessage ?? "The entity workspace could not be loaded."}
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
            <h1>{entity.name}</h1>
            <p className="quartz-page-subtitle">
              Entity Home • Operational Period:{" "}
              {activeCloseRun
                ? formatCloseRunPeriod(activeCloseRun)
                : "No active close in progress"}
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <button
              className="secondary-button quartz-toolbar-button"
              onClick={() => openUploadDialog()}
              type="button"
            >
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Upload COA / GL / TB
            </button>
            <button className="primary-button" onClick={openCreateCloseRunDialog} type="button">
              Start Close Run
            </button>
          </div>
        </header>

        {entityErrorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {entityErrorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid quartz-kpi-grid-triple">
            {metricTiles.map((tile, index) => (
              <article
                className={
                  index === metricTiles.length - 1 ? "quartz-kpi-tile highlight" : "quartz-kpi-tile"
                }
                key={tile.label}
              >
                <p className="quartz-kpi-label">{tile.label}</p>
                <p className={`quartz-kpi-value ${tile.tone ?? ""}`.trim()}>{tile.value}</p>
                <p className="quartz-kpi-meta">{tile.meta}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Close Run Ledger</h2>
            <Link className="quartz-filter-link" href="/entities">
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Directory
            </Link>
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Period</th>
                  <th>Status</th>
                  <th>Current Gate</th>
                  <th>Last Updated</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {closeRuns.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="quartz-empty-state">
                        No close runs exist yet for this workspace.
                      </div>
                    </td>
                  </tr>
                ) : (
                  closeRuns.map((closeRun) => {
                    const activePhase = findActivePhase(closeRun);
                    const rowAttention = deriveCloseRunAttention(closeRun);
                    const badgeTone =
                      rowAttention.tone === "warning"
                        ? "error"
                        : closeRun.status === "approved" || closeRun.status === "archived"
                          ? "success"
                          : "neutral";

                    return (
                      <tr
                        className={
                          rowAttention.tone === "warning" ? "quartz-table-row error" : undefined
                        }
                        key={closeRun.id}
                      >
                        <td>
                          <div className="quartz-table-primary">
                            {formatCloseRunPeriod(closeRun)}
                          </div>
                          <div className="quartz-table-secondary">
                            v{closeRun.currentVersionNo} • {closeRun.reportingCurrency}
                          </div>
                        </td>
                        <td>
                          <span className={`quartz-status-badge ${badgeTone}`}>
                            {getCloseRunStatusLabel(closeRun.status)}
                          </span>
                        </td>
                        <td>
                          <div className="quartz-table-primary">
                            {activePhase?.phase ? formatWorkflowPhaseLabel(activePhase.phase) : "Complete"}
                          </div>
                          <div className="quartz-table-secondary">{rowAttention.detail}</div>
                        </td>
                        <td>{formatCloseRunDateTime(closeRun.updatedAt)}</td>
                        <td className="quartz-table-center">
                          <Link
                            className="quartz-action-link"
                            href={`/entities/${entity.id}/close-runs/${closeRun.id}`}
                          >
                            Open
                          </Link>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </section>

      {isCreateCloseRunDialogOpen ? (
        <div
          aria-modal="true"
          className="quartz-modal-backdrop"
          onClick={closeCreateCloseRunDialog}
          role="dialog"
        >
          <div
            className="quartz-modal-card"
            onClick={(event) => event.stopPropagation()}
            role="document"
          >
            <div className="quartz-section-header quartz-section-header-tight">
              <div>
                <h2 className="quartz-section-title">Start Close Run</h2>
                <p className="quartz-page-subtitle">
                  Select the reporting period for {entity.name}.
                </p>
              </div>
              <button
                aria-label="Close"
                className="quartz-icon-button"
                onClick={closeCreateCloseRunDialog}
                type="button"
              >
                <QuartzIcon name="close" />
              </button>
            </div>

            <form className="quartz-setup-form" onSubmit={handleCreateCloseRun}>
              <div className="quartz-form-grid">
                <label className="quartz-form-label">
                  <span>Period Start</span>
                  <input
                    className="text-input"
                    onChange={handleCloseRunFieldChange("periodStart")}
                    required
                    type="date"
                    value={closeRunFormState.periodStart}
                  />
                </label>

                <label className="quartz-form-label">
                  <span>Period End</span>
                  <input
                    className="text-input"
                    onChange={handleCloseRunFieldChange("periodEnd")}
                    required
                    type="date"
                    value={closeRunFormState.periodEnd}
                  />
                </label>
              </div>

              {closeRunErrorMessage ? (
                <div className="status-banner warning" role="status">
                  {closeRunErrorMessage}
                </div>
              ) : null}

              <div className="quartz-form-row quartz-modal-actions">
                <button
                  className="secondary-button"
                  onClick={closeCreateCloseRunDialog}
                  type="button"
                >
                  Cancel
                </button>
                <button className="primary-button" disabled={isPending} type="submit">
                  {isPending ? "Creating close run..." : "Start Close Run"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}

      {isUploadDialogOpen ? (
        <div
          aria-modal="true"
          className="quartz-modal-backdrop"
          onClick={closeUploadDialog}
          role="dialog"
        >
          <div
            className="quartz-modal-card quartz-modal-card-wide quartz-entity-upload-modal"
            onClick={(event) => event.stopPropagation()}
            role="document"
          >
            <div className="quartz-section-header quartz-section-header-tight">
              <div>
                <h2 className="quartz-section-title">Upload Entity Data</h2>
                <p className="quartz-page-subtitle">
                  Upload chart of accounts, general ledger, or trial balance without leaving{" "}
                  {entity.name}.
                </p>
              </div>
              <button
                aria-label="Close"
                className="quartz-icon-button"
                onClick={closeUploadDialog}
                type="button"
              >
                <QuartzIcon name="close" />
              </button>
            </div>

            {activeCloseRun ? (
              <div className="quartz-inline-note" role="status">
                Current close period defaults are ready for ledger uploads:{" "}
                {formatCloseRunPeriod(activeCloseRun)}.
              </div>
            ) : null}

            <div className="quartz-upload-dataset-grid" role="tablist">
              {uploadDatasetCards.map((card) => (
                <button
                  aria-selected={selectedUploadKind === card.key}
                  className={`quartz-upload-dataset-card ${selectedUploadKind === card.key ? "active" : ""}`}
                  key={card.key}
                  onClick={() => {
                    setSelectedUploadKind(card.key);
                    setUploadDialogErrorMessage(null);
                  }}
                  role="tab"
                  type="button"
                >
                  <span className="quartz-kpi-label">{card.label}</span>
                  <strong>{card.value}</strong>
                  <span className="quartz-upload-dataset-meta">{card.meta}</span>
                </button>
              ))}
            </div>

            {isUploadWorkspaceLoading ? (
              <div className="quartz-inline-note">Loading entity data state...</div>
            ) : null}

            {uploadStatusMessage ? (
              <div className="status-banner success" role="status">
                {uploadStatusMessage}
              </div>
            ) : null}

            {uploadDialogErrorMessage ? (
              <div className="status-banner warning" role="status">
                {uploadDialogErrorMessage}
              </div>
            ) : null}

            {selectedUploadKind === "coa" ? (
              <section className="quartz-upload-panel">
                <div className="quartz-upload-panel-header">
                  <div>
                    <h3 className="quartz-upload-panel-title">Chart of Accounts</h3>
                    <p className="quartz-page-subtitle">
                      Replace or activate the entity account structure from a CSV or Excel file.
                    </p>
                  </div>
                  <div className="quartz-upload-panel-meta">
                    <span className="quartz-upload-chip">
                      {activeCoaSet ? `v${activeCoaSet.version_no}` : "No active set"}
                    </span>
                    <span className="quartz-upload-chip">
                      {activeCoaSet?.account_count ?? 0} accounts
                    </span>
                    <span className="quartz-upload-chip">
                      {formatCoaSourceLabel(activeCoaSet?.source ?? null)}
                    </span>
                  </div>
                </div>

                <form className="quartz-setup-form" onSubmit={handleUploadCoa}>
                  <label className="quartz-form-label quartz-upload-file-field">
                    <span>COA File</span>
                    <input
                      accept=".csv,.xlsx,.xlsm"
                      onChange={handleUploadFileChange("coa")}
                      type="file"
                    />
                  </label>
                  <div className="quartz-inline-note">
                    {coaFile ? `Selected file: ${coaFile.name}` : "Accepted formats: CSV, XLSX, XLSM."}
                  </div>

                  <div className="quartz-form-row quartz-modal-actions">
                    <button className="secondary-button" onClick={closeUploadDialog} type="button">
                      Cancel
                    </button>
                    <button className="primary-button" disabled={isUploadPending} type="submit">
                      {isUploadPending ? "Uploading..." : "Upload Chart of Accounts"}
                    </button>
                  </div>
                </form>
              </section>
            ) : null}

            {selectedUploadKind === "general_ledger" ? (
              <section className="quartz-upload-panel">
                <div className="quartz-upload-panel-header">
                  <div>
                    <h3 className="quartz-upload-panel-title">General Ledger</h3>
                    <p className="quartz-page-subtitle">
                      Upload the baseline ledger for the period you want tied to eligible close runs.
                    </p>
                  </div>
                  <div className="quartz-upload-panel-meta">
                    <span className="quartz-upload-chip">
                      {ledgerWorkspace?.general_ledger_imports.length ?? 0} imports
                    </span>
                    <span className="quartz-upload-chip">
                      {latestGeneralLedgerImport?.uploaded_filename ?? "No import yet"}
                    </span>
                    <span className="quartz-upload-chip">
                      {latestGeneralLedgerImport
                        ? `${latestGeneralLedgerImport.period_start} to ${latestGeneralLedgerImport.period_end}`
                        : "Period not set"}
                    </span>
                  </div>
                </div>

                <form className="quartz-setup-form" onSubmit={handleUploadGeneralLedger}>
                  <div className="quartz-form-grid">
                    <label className="quartz-form-label">
                      <span>Period Start</span>
                      <input
                        className="text-input"
                        onChange={handleUploadPeriodFieldChange("gl", "periodStart")}
                        required
                        type="date"
                        value={glUploadFormState.periodStart}
                      />
                    </label>
                    <label className="quartz-form-label">
                      <span>Period End</span>
                      <input
                        className="text-input"
                        onChange={handleUploadPeriodFieldChange("gl", "periodEnd")}
                        required
                        type="date"
                        value={glUploadFormState.periodEnd}
                      />
                    </label>
                  </div>

                  <label className="quartz-form-label quartz-upload-file-field">
                    <span>General Ledger File</span>
                    <input
                      accept=".csv,.xlsx,.xlsm"
                      onChange={handleUploadFileChange("general_ledger")}
                      type="file"
                    />
                  </label>
                  <div className="quartz-inline-note">
                    {glFile ? `Selected file: ${glFile.name}` : "Accepted formats: CSV, XLSX, XLSM."}
                  </div>

                  <div className="quartz-form-row quartz-modal-actions">
                    <button className="secondary-button" onClick={closeUploadDialog} type="button">
                      Cancel
                    </button>
                    <button className="primary-button" disabled={isUploadPending} type="submit">
                      {isUploadPending ? "Uploading..." : "Upload General Ledger"}
                    </button>
                  </div>
                </form>
              </section>
            ) : null}

            {selectedUploadKind === "trial_balance" ? (
              <section className="quartz-upload-panel">
                <div className="quartz-upload-panel-header">
                  <div>
                    <h3 className="quartz-upload-panel-title">Trial Balance</h3>
                    <p className="quartz-page-subtitle">
                      Upload the trial balance for the selected period and bind it to eligible close
                      runs.
                    </p>
                  </div>
                  <div className="quartz-upload-panel-meta">
                    <span className="quartz-upload-chip">
                      {ledgerWorkspace?.trial_balance_imports.length ?? 0} imports
                    </span>
                    <span className="quartz-upload-chip">
                      {latestTrialBalanceImport?.uploaded_filename ?? "No import yet"}
                    </span>
                    <span className="quartz-upload-chip">
                      {latestTrialBalanceImport
                        ? `${latestTrialBalanceImport.period_start} to ${latestTrialBalanceImport.period_end}`
                        : "Period not set"}
                    </span>
                  </div>
                </div>

                <form className="quartz-setup-form" onSubmit={handleUploadTrialBalance}>
                  <div className="quartz-form-grid">
                    <label className="quartz-form-label">
                      <span>Period Start</span>
                      <input
                        className="text-input"
                        onChange={handleUploadPeriodFieldChange("tb", "periodStart")}
                        required
                        type="date"
                        value={tbUploadFormState.periodStart}
                      />
                    </label>
                    <label className="quartz-form-label">
                      <span>Period End</span>
                      <input
                        className="text-input"
                        onChange={handleUploadPeriodFieldChange("tb", "periodEnd")}
                        required
                        type="date"
                        value={tbUploadFormState.periodEnd}
                      />
                    </label>
                  </div>

                  <label className="quartz-form-label quartz-upload-file-field">
                    <span>Trial Balance File</span>
                    <input
                      accept=".csv,.xlsx,.xlsm"
                      onChange={handleUploadFileChange("trial_balance")}
                      type="file"
                    />
                  </label>
                  <div className="quartz-inline-note">
                    {tbFile ? `Selected file: ${tbFile.name}` : "Accepted formats: CSV, XLSX, XLSM."}
                  </div>

                  <div className="quartz-form-row quartz-modal-actions">
                    <button className="secondary-button" onClick={closeUploadDialog} type="button">
                      Cancel
                    </button>
                    <button className="primary-button" disabled={isUploadPending} type="submit">
                      {isUploadPending ? "Uploading..." : "Upload Trial Balance"}
                    </button>
                  </div>
                </form>
              </section>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

async function loadWorkspaceView(options: {
  entityId: string;
  onCloseRunError: (message: string | null) => void;
  onCloseRunsLoaded: (closeRuns: readonly CloseRunSummary[]) => void;
  onError: (message: string | null) => void;
  onLoaded: (workspace: EntityWorkspace) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  const [workspaceResult, closeRunsResult] = await Promise.allSettled([
    readEntityWorkspace(options.entityId),
    listCloseRuns(options.entityId),
  ]);

  try {
    if (workspaceResult.status === "rejected") {
      throw workspaceResult.reason;
    }

    options.onLoaded(workspaceResult.value);
    options.onError(null);

    if (closeRunsResult.status === "fulfilled") {
      options.onCloseRunsLoaded(closeRunsResult.value);
      options.onCloseRunError(null);
    } else {
      options.onCloseRunsLoaded([]);
      options.onCloseRunError(resolveWorkspaceViewErrorMessage(closeRunsResult.reason));
    }
  } catch (error: unknown) {
    options.onCloseRunsLoaded([]);
    options.onCloseRunError(null);
    options.onError(resolveWorkspaceViewErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveWorkspaceViewErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError || error instanceof CloseRunApiError) {
    return error.message;
  }

  return "The entity workspace request failed. Reload the page and try again.";
}

function findWorkingCloseRun(closeRuns: readonly CloseRunSummary[]): CloseRunSummary | null {
  return (
    closeRuns.find((closeRun) => ["draft", "in_review", "reopened"].includes(closeRun.status)) ??
    closeRuns[0] ??
    null
  );
}

function buildLedgerUploadStatusMessage(
  label: string,
  response: Readonly<LedgerImportUploadResponse>,
): string {
  if (
    response.auto_bound_close_run_ids.length === 0 &&
    response.skipped_close_run_ids.length === 0
  ) {
    return `${label} uploaded successfully.`;
  }

  if (response.skipped_close_run_ids.length === 0) {
    return `${label} uploaded and auto-bound to ${response.auto_bound_close_run_ids.length} close run(s).`;
  }

  if (response.auto_bound_close_run_ids.length === 0) {
    return (
      `${label} uploaded, but ${response.skipped_close_run_ids.length} matching close run(s) were `
      + "left unbound because they already have ledger activity."
    );
  }

  return (
    `${label} uploaded, auto-bound to ${response.auto_bound_close_run_ids.length} close run(s), `
    + `and skipped ${response.skipped_close_run_ids.length} started close run(s).`
  );
}

function resolvePreferredUploadKind(
  coaWorkspace: Readonly<CoaWorkspaceResponse> | null,
  ledgerWorkspace: Readonly<LedgerWorkspaceResponse> | null,
): UploadEntityDataKind {
  if ((coaWorkspace?.active_set.account_count ?? 0) === 0) {
    return "coa";
  }

  if ((ledgerWorkspace?.general_ledger_imports.length ?? 0) === 0) {
    return "general_ledger";
  }

  if ((ledgerWorkspace?.trial_balance_imports.length ?? 0) === 0) {
    return "trial_balance";
  }

  return "general_ledger";
}

function buildUploadDatasetCards(options: {
  activeCoaSet: Readonly<CoaWorkspaceResponse["active_set"]> | null;
  latestGeneralLedgerImport: Readonly<LedgerWorkspaceResponse["general_ledger_imports"][number]> | null;
  latestTrialBalanceImport: Readonly<LedgerWorkspaceResponse["trial_balance_imports"][number]> | null;
}): readonly UploadDatasetCard[] {
  return [
    {
      key: "coa",
      label: "Chart of Accounts",
      meta: options.activeCoaSet
        ? `${options.activeCoaSet.account_count} accounts`
        : "No active set",
      value: options.activeCoaSet ? `v${options.activeCoaSet.version_no}` : "Upload required",
    },
    {
      key: "general_ledger",
      label: "General Ledger",
      meta: options.latestGeneralLedgerImport
        ? `${options.latestGeneralLedgerImport.period_start} to ${options.latestGeneralLedgerImport.period_end}`
        : "No import on record",
      value: options.latestGeneralLedgerImport?.uploaded_filename ?? "Upload required",
    },
    {
      key: "trial_balance",
      label: "Trial Balance",
      meta: options.latestTrialBalanceImport
        ? `${options.latestTrialBalanceImport.period_start} to ${options.latestTrialBalanceImport.period_end}`
        : "No import on record",
      value: options.latestTrialBalanceImport?.uploaded_filename ?? "Upload required",
    },
  ];
}

function buildEntityMetricTiles(
  entity: Readonly<EntityWorkspace> | null,
  closeRuns: readonly CloseRunSummary[],
): readonly MetricTile[] {
  const openCloseRuns = closeRuns.filter((closeRun) =>
    ["draft", "in_review", "reopened"].includes(closeRun.status),
  ).length;
  const blockedControls = closeRuns.reduce(
    (count, closeRun) =>
      count +
      closeRun.workflowState.phaseStates.filter((phaseState) => phaseState.status === "blocked")
        .length,
    0,
  );
  return [
    {
      label: "Open Periods",
      meta: `${closeRuns.length} close runs on record`,
      value: String(openCloseRuns),
    },
    {
      label: "Blocked Controls",
      meta:
        blockedControls > 0
          ? "Escalate blocked review gates before sign-off"
          : "No blocked workflow gates across active periods",
      tone: blockedControls > 0 ? "error" : "success",
      value: String(blockedControls),
    },
    {
      label: "Workspace Members",
      meta: entity?.default_actor?.full_name ?? "Default actor not assigned",
      value: String(entity?.member_count ?? 0),
    },
  ];
}

function formatWorkflowPhaseLabel(phase: CloseRunSummary["workflowState"]["phaseStates"][number]["phase"]): string {
  switch (phase) {
    case "collection":
      return "Inputs";
    case "processing":
      return "Journals";
    case "reconciliation":
      return "Reconciliation";
    case "reporting":
      return "Reports";
    case "review_signoff":
      return "Sign-Off";
  }
}

function formatCoaSourceLabel(
  source: CoaWorkspaceResponse["active_set"]["source"] | null,
): string {
  switch (source) {
    case "manual_upload":
      return "Manual upload";
    case "quickbooks_sync":
      return "QuickBooks sync";
    case "fallback_nigerian_sme":
      return "Fallback set";
    default:
      return "Not uploaded";
  }
}

function resolveEntityDataUploadError(error: unknown): string {
  if (error instanceof CoaApiError || error instanceof LedgerApiError) {
    return error.message;
  }

  if (error instanceof Error && error.message.length > 0) {
    return error.message;
  }

  return "The entity data upload could not be completed. Retry the upload.";
}
