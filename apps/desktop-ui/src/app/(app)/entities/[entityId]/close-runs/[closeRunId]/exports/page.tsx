"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../../../../../components/layout/QuartzIcons";
import {
  approveCloseRun,
  CloseRunApiError,
  formatCloseRunPeriod,
  readCloseRunWorkspace,
  type CloseRunSummary,
  type CloseRunWorkspaceData,
} from "../../../../../../../lib/close-runs";
import {
  assembleEvidencePack,
  buildEvidencePackDownloadPath,
  distributeExport,
  type EvidencePackBundle,
  EXPORT_DELIVERY_CHANNELS,
  type ExportArtifactEntry,
  type ExportDetail,
  type ExportSummary,
  ExportApiError,
  listExports,
  readExportDetail,
  readLatestEvidencePack,
  triggerExport,
} from "../../../../../../../lib/exports";
import {
  buildGeneralLedgerExportDownloadPath,
  generateGeneralLedgerExport,
  type GeneralLedgerExportSummary,
  LedgerApiError,
  readLatestGeneralLedgerExport,
} from "../../../../../../../lib/ledger";
import {
  buildReportArtifactDownloadPath,
  listReportRuns,
  readReportRun,
  ReportApiError,
} from "../../../../../../../lib/reports";

type CloseRunExportsPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

type DistributionFormState = {
  deliveryChannel: string;
  note: string;
  recipientEmail: string;
  recipientName: string;
  recipientRole: string;
};

type ReleaseArtifactRow = Readonly<{
  actionHref: string | null;
  actionLabel: string | null;
  filename: string;
  format: string;
  key: string;
  sourceLabel: string;
  statusLabel: string;
  statusTone: "error" | "neutral" | "success" | "warning";
}>;

type ReadinessMarker = Readonly<{
  complete: boolean;
  detail: string;
  label: string;
  value: string;
}>;

const defaultDistributionFormState: DistributionFormState = {
  deliveryChannel: "secure_email",
  note: "",
  recipientEmail: "",
  recipientName: "",
  recipientRole: "",
};

const emptyExports: readonly ExportSummary[] = [];
export default function CloseRunExportsPage({
  params,
}: Readonly<CloseRunExportsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);

  const [closeRunWorkspace, setCloseRunWorkspace] = useState<CloseRunWorkspaceData | null>(null);
  const [distributionForm, setDistributionForm] = useState<DistributionFormState>(
    defaultDistributionFormState,
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [exportDetail, setExportDetail] = useState<ExportDetail | null>(null);
  const [exports, setExports] = useState<readonly ExportSummary[]>(emptyExports);
  const [generalLedgerExport, setGeneralLedgerExport] = useState<GeneralLedgerExportSummary | null>(
    null,
  );
  const [isApprovingCloseRun, setIsApprovingCloseRun] = useState(false);
  const [isAssemblingEvidence, setIsAssemblingEvidence] = useState(false);
  const [isDistributingExport, setIsDistributingExport] = useState(false);
  const [isGeneratingGeneralLedgerExport, setIsGeneratingGeneralLedgerExport] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isTriggeringExport, setIsTriggeringExport] = useState(false);
  const [latestEvidencePack, setLatestEvidencePack] = useState<EvidencePackBundle | null>(null);
  const [latestReportRun, setLatestReportRun] = useState<Awaited<
    ReturnType<typeof readReportRun>
  > | null>(null);
  const [selectedExportId, setSelectedExportId] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const loadExportsWorkspace = useCallback(
    async (preferredExportId?: string): Promise<void> => {
      setIsLoading(true);
      try {
        const [
          nextCloseRunWorkspace,
          nextExports,
          nextEvidencePack,
          nextGeneralLedgerExport,
          nextReportRuns,
        ] = await Promise.all([
          readCloseRunWorkspace(entityId, closeRunId),
          listExports(entityId, closeRunId),
          readLatestEvidencePack(entityId, closeRunId),
          readLatestGeneralLedgerExport(entityId, closeRunId),
          listReportRuns(entityId, closeRunId),
        ]);

        const nextSelectedExportId =
          preferredExportId ?? selectedExportId ?? nextExports[0]?.id ?? null;
        const [nextExportDetail, nextReportRunDetail] = await Promise.all([
          nextSelectedExportId === null
            ? Promise.resolve(null)
            : readExportDetail(entityId, closeRunId, nextSelectedExportId),
          nextReportRuns[0] === undefined
            ? Promise.resolve(null)
            : readReportRun(entityId, closeRunId, nextReportRuns[0].id),
        ]);

        setCloseRunWorkspace(nextCloseRunWorkspace);
        setExports(nextExports);
        setLatestEvidencePack(nextEvidencePack);
        setGeneralLedgerExport(nextGeneralLedgerExport);
        setLatestReportRun(nextReportRunDetail);
        setSelectedExportId(nextSelectedExportId);
        setExportDetail(nextExportDetail);
        setErrorMessage(null);
      } catch (error: unknown) {
        setErrorMessage(resolveExportErrorMessage(error));
      } finally {
        setIsLoading(false);
      }
    },
    [closeRunId, entityId, selectedExportId],
  );

  useEffect(() => {
    void loadExportsWorkspace();
  }, [loadExportsWorkspace]);

  const readinessMarkers = useMemo(
    () =>
      closeRunWorkspace === null
        ? []
        : buildReadinessMarkers(closeRunWorkspace.closeRun, latestReportRun, latestEvidencePack),
    [closeRunWorkspace, latestEvidencePack, latestReportRun],
  );
  const releaseReady = readinessMarkers.every((marker) => marker.complete);
  const artifactRows = useMemo(
    () =>
      buildReleaseArtifactRows({
        closeRunId,
        entityId,
        exportDetail,
        generalLedgerExport,
        latestEvidencePack,
        latestReportRun,
      }),
    [closeRunId, entityId, exportDetail, generalLedgerExport, latestEvidencePack, latestReportRun],
  );

  async function handleTriggerExport(): Promise<void> {
    setIsTriggeringExport(true);
    setStatusMessage(null);
    try {
      const detail = await triggerExport(entityId, closeRunId);
      setStatusMessage(`Export created for version ${detail.version_no}.`);
      await loadExportsWorkspace(detail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsTriggeringExport(false);
    }
  }

  async function handleGenerateGeneralLedgerExport(): Promise<void> {
    setIsGeneratingGeneralLedgerExport(true);
    setStatusMessage(null);
    try {
      const summary = await generateGeneralLedgerExport(entityId, closeRunId);
      setGeneralLedgerExport(summary);
      setStatusMessage(`GL export ready: ${summary.filename}.`);
      setErrorMessage(null);
      await loadExportsWorkspace(selectedExportId ?? undefined);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsGeneratingGeneralLedgerExport(false);
    }
  }

  async function handleAssembleEvidencePack(): Promise<void> {
    setIsAssemblingEvidence(true);
    setStatusMessage(null);
    try {
      const evidencePack = await assembleEvidencePack(entityId, closeRunId);
      setLatestEvidencePack(evidencePack);
      setStatusMessage(`Evidence pack ready for version ${evidencePack.version_no}.`);
      setErrorMessage(null);
      await loadExportsWorkspace(selectedExportId ?? undefined);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsAssemblingEvidence(false);
    }
  }

  async function handleSelectExport(exportId: string): Promise<void> {
    setStatusMessage(null);
    await loadExportsWorkspace(exportId);
  }

  async function handleDistributeExport(): Promise<void> {
    if (exportDetail === null) {
      setErrorMessage("Create or select a completed export before recording distribution.");
      return;
    }

    setIsDistributingExport(true);
    setStatusMessage(null);
    try {
      const detail = await distributeExport(entityId, closeRunId, exportDetail.id, {
        delivery_channel: distributionForm.deliveryChannel,
        note: distributionForm.note || null,
        recipient_email: distributionForm.recipientEmail,
        recipient_name: distributionForm.recipientName,
        recipient_role: distributionForm.recipientRole || null,
      });
      setDistributionForm(defaultDistributionFormState);
      setStatusMessage(
        `Distribution recorded for ${detail.distribution_records.at(-1)?.recipient_name ?? "stakeholder"}.`,
      );
      await loadExportsWorkspace(detail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsDistributingExport(false);
    }
  }

  async function handleApproveCloseRun(): Promise<void> {
    if (closeRunWorkspace === null) {
      return;
    }

    setIsApprovingCloseRun(true);
    setStatusMessage(null);
    try {
      await approveCloseRun(entityId, closeRunId, "Approved from sign-off and release workspace");
      setStatusMessage("Close run approved.");
      await loadExportsWorkspace(selectedExportId ?? undefined);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsApprovingCloseRun(false);
    }
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading sign-off and release...</div>
        </section>
      </div>
    );
  }

  if (closeRunWorkspace === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The sign-off and release workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  const closeRun = closeRunWorkspace.closeRun;
  const entityName = closeRunWorkspace.entity.name;
  const statusBadge = resolveReleaseStatusLabel(closeRun, releaseReady);

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <span className="quartz-status-badge neutral">{statusBadge}</span>
            <h1>Sign-Off and Release</h1>
            <p className="quartz-page-subtitle">
              {entityName} • {formatCloseRunPeriod(closeRun)}
            </p>
          </div>

          <div className="quartz-page-toolbar">
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/chat`}
            >
              <QuartzIcon className="quartz-inline-icon" name="assistant" />
              Open Assistant
            </Link>
            {closeRun.status === "approved" || closeRun.status === "archived" ? (
              <Link
                className="secondary-button"
                href={`/entities/${entityId}/close-runs/${closeRunId}/complete`}
              >
                Open Completion Summary
              </Link>
            ) : null}
            <button
              className="primary-button"
              disabled={
                isApprovingCloseRun ||
                closeRun.status === "approved" ||
                closeRun.status === "archived"
              }
              onClick={() => {
                void handleApproveCloseRun();
              }}
              type="button"
            >
              {closeRun.status === "approved" || closeRun.status === "archived"
                ? "Close Run Approved"
                : isApprovingCloseRun
                  ? "Approving..."
                  : "Approve Close Run"}
            </button>
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

        <section className="quartz-section quartz-release-layout">
          <div className="quartz-release-side">
            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-section-title">Readiness Markers</h2>
                <span className={`quartz-status-badge ${releaseReady ? "success" : "warning"}`}>
                  {releaseReady ? "Ready" : "In progress"}
                </span>
              </div>

              <div className="quartz-release-marker-list">
                {readinessMarkers.map((marker) => (
                  <div className="quartz-release-marker" key={marker.label}>
                    <div className="quartz-release-marker-label">
                      <span
                        className={`quartz-icon-badge ${marker.complete ? "success" : "warning"}`}
                      >
                        <QuartzIcon
                          className="quartz-inline-icon"
                          name={marker.complete ? "check" : "warning"}
                        />
                      </span>
                      <div>
                        <strong>{marker.label}</strong>
                        <span>{marker.detail}</span>
                      </div>
                    </div>
                    <strong>{marker.value}</strong>
                  </div>
                ))}
              </div>
            </article>

            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-section-title">Distribution Record</h2>
                <span className="quartz-queue-meta">
                  {exportDetail?.distribution_records.length ?? 0} release
                  {(exportDetail?.distribution_records.length ?? 0) === 1 ? "" : "s"}
                </span>
              </div>

              {exportDetail === null ? (
                <div className="quartz-empty-state">
                  Create the current export package before recording stakeholder distribution.
                </div>
              ) : (
                <div className="quartz-release-distribution-stack">
                  <div className="quartz-mini-list">
                    {exportDetail.distribution_records.length === 0 ? (
                      <div className="quartz-mini-item">
                        <strong>No distribution recorded yet</strong>
                        <span className="quartz-mini-meta">
                          Record the final release recipients against the completed export package.
                        </span>
                      </div>
                    ) : (
                      exportDetail.distribution_records.map((record) => (
                        <div className="quartz-mini-item" key={record.id}>
                          <strong>{record.recipient_name}</strong>
                          <span className="quartz-mini-meta">
                            {record.recipient_role ?? "Stakeholder"} •{" "}
                            {formatChannelLabel(record.delivery_channel)} •{" "}
                            {formatTimestamp(record.distributed_at)}
                          </span>
                        </div>
                      ))
                    )}
                  </div>

                  <div className="quartz-divider" />

                  <div className="quartz-compact-form">
                    <label>
                      <span className="quartz-kpi-label">Recipient name</span>
                      <input
                        className="text-input"
                        onChange={(event) =>
                          setDistributionForm((current) => ({
                            ...current,
                            recipientName: event.target.value,
                          }))
                        }
                        placeholder="Amina Yusuf"
                        type="text"
                        value={distributionForm.recipientName}
                      />
                    </label>
                    <label>
                      <span className="quartz-kpi-label">Recipient email</span>
                      <input
                        className="text-input"
                        onChange={(event) =>
                          setDistributionForm((current) => ({
                            ...current,
                            recipientEmail: event.target.value,
                          }))
                        }
                        placeholder="cfo@example.com"
                        type="email"
                        value={distributionForm.recipientEmail}
                      />
                    </label>
                    <label>
                      <span className="quartz-kpi-label">Role</span>
                      <input
                        className="text-input"
                        onChange={(event) =>
                          setDistributionForm((current) => ({
                            ...current,
                            recipientRole: event.target.value,
                          }))
                        }
                        placeholder="Chief Financial Officer"
                        type="text"
                        value={distributionForm.recipientRole}
                      />
                    </label>
                    <label>
                      <span className="quartz-kpi-label">Delivery channel</span>
                      <select
                        className="text-input"
                        onChange={(event) =>
                          setDistributionForm((current) => ({
                            ...current,
                            deliveryChannel: event.target.value,
                          }))
                        }
                        value={distributionForm.deliveryChannel}
                      >
                        {EXPORT_DELIVERY_CHANNELS.map((channel) => (
                          <option key={channel} value={channel}>
                            {formatChannelLabel(channel)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span className="quartz-kpi-label">Operator note</span>
                      <textarea
                        className="text-input quartz-compact-textarea"
                        onChange={(event) =>
                          setDistributionForm((current) => ({
                            ...current,
                            note: event.target.value,
                          }))
                        }
                        placeholder="Board pack released after final controller review."
                        value={distributionForm.note}
                      />
                    </label>
                  </div>

                  <button
                    className="secondary-button"
                    disabled={
                      isDistributingExport ||
                      exportDetail.status !== "completed" ||
                      distributionForm.recipientName.trim().length === 0 ||
                      distributionForm.recipientEmail.trim().length === 0
                    }
                    onClick={() => {
                      void handleDistributeExport();
                    }}
                    type="button"
                  >
                    {isDistributingExport ? "Recording..." : "Record distribution"}
                  </button>
                </div>
              )}
            </article>
          </div>

          <div className="quartz-release-main">
            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <h2 className="quartz-section-title">Master Evidence Pack</h2>
                  <p className="quartz-table-secondary">
                    {latestEvidencePack
                      ? `Version ${latestEvidencePack.version_no}`
                      : "Not yet assembled"}
                  </p>
                </div>
                <span
                  className={`quartz-status-badge ${latestEvidencePack ? "success" : "warning"}`}
                >
                  {latestEvidencePack ? "Verified" : "Pending"}
                </span>
              </div>

              {latestEvidencePack === null ? (
                <div className="quartz-empty-state">
                  Release the evidence pack to bundle source references, extracted values, and final
                  outputs for audit coverage.
                </div>
              ) : (
                <div className="quartz-release-evidence-layout">
                  <div className="quartz-evidence-file-box">
                    <QuartzIcon className="quartz-release-evidence-icon" name="close" />
                  </div>
                  <div className="quartz-release-evidence-copy">
                    <ul className="quartz-release-evidence-list">
                      {latestEvidencePack.items.slice(0, 4).map((item) => (
                        <li key={`${item.item_type}:${item.label}`}>
                          {item.label || formatLabel(item.item_type)}
                        </li>
                      ))}
                    </ul>
                    <p className="quartz-mini-meta">
                      Generated {formatTimestamp(latestEvidencePack.generated_at)} •{" "}
                      {formatBytes(latestEvidencePack.size_bytes ?? 0)}
                    </p>
                  </div>
                </div>
              )}

              <div className="quartz-inline-action-row">
                <button
                  className="secondary-button"
                  disabled={isAssemblingEvidence}
                  onClick={() => {
                    void handleAssembleEvidencePack();
                  }}
                  type="button"
                >
                  {isAssemblingEvidence ? "Assembling..." : "Assemble evidence pack"}
                </button>
                {latestEvidencePack ? (
                  <a
                    className="secondary-button"
                    href={buildEvidencePackDownloadPath(entityId, closeRunId)}
                  >
                    Download evidence pack
                  </a>
                ) : null}
              </div>
            </article>

            <article className="quartz-card quartz-card-table-shell">
              <div className="quartz-section-header">
                <div>
                  <h2 className="quartz-section-title">Export Artifacts</h2>
                  <p className="quartz-table-secondary">
                    {exportDetail
                      ? `Export v${exportDetail.version_no} • ${formatLabel(exportDetail.status)}`
                      : "No export selected"}
                  </p>
                </div>
                <div className="quartz-inline-action-row">
                  <button
                    className="secondary-button"
                    disabled={isTriggeringExport}
                    onClick={() => {
                      void handleTriggerExport();
                    }}
                    type="button"
                  >
                    {isTriggeringExport ? "Exporting..." : "Create export"}
                  </button>
                  <button
                    className="secondary-button"
                    disabled={isGeneratingGeneralLedgerExport}
                    onClick={() => {
                      void handleGenerateGeneralLedgerExport();
                    }}
                    type="button"
                  >
                    {isGeneratingGeneralLedgerExport ? "Generating..." : "Generate GL export"}
                  </button>
                </div>
              </div>

              {exports.length > 1 ? (
                <div className="quartz-filter-chip-row" style={{ padding: "0 16px 16px" }}>
                  {exports.map((exportRun) => (
                    <button
                      className={
                        selectedExportId === exportRun.id
                          ? "quartz-filter-chip active"
                          : "quartz-filter-chip"
                      }
                      key={exportRun.id}
                      onClick={() => {
                        void handleSelectExport(exportRun.id);
                      }}
                      type="button"
                    >
                      v{exportRun.version_no}
                    </button>
                  ))}
                </div>
              ) : null}

              <table className="quartz-table">
                <thead>
                  <tr>
                    <th>Artifact name</th>
                    <th>Format</th>
                    <th>Status</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {artifactRows.length === 0 ? (
                    <tr>
                      <td colSpan={4}>
                        <div className="quartz-empty-state">
                          Create the export package and release the supporting artifacts to populate
                          the manifest.
                        </div>
                      </td>
                    </tr>
                  ) : (
                    artifactRows.map((row) => (
                      <tr key={row.key}>
                        <td>
                          <div className="quartz-table-primary">{row.filename}</div>
                          <div className="quartz-table-secondary">{row.sourceLabel}</div>
                        </td>
                        <td>{row.format}</td>
                        <td>
                          <span className={`quartz-status-badge ${row.statusTone}`}>
                            {row.statusLabel}
                          </span>
                        </td>
                        <td>
                          {row.actionHref ? (
                            <a className="quartz-action-link" href={row.actionHref}>
                              {row.actionLabel ?? "Download"}
                            </a>
                          ) : (
                            <span className="quartz-table-secondary">Manifest only</span>
                          )}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>

              {exportDetail?.failure_reason ? (
                <div className="status-banner warning" role="status">
                  {exportDetail.failure_reason}
                </div>
              ) : null}
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}

function buildReadinessMarkers(
  closeRun: Readonly<CloseRunSummary>,
  latestReportRun: Awaited<ReturnType<typeof readReportRun>> | null,
  latestEvidencePack: EvidencePackBundle | null,
): readonly ReadinessMarker[] {
  const phaseByCode = new Map(
    closeRun.workflowState.phaseStates.map((phaseState) => [phaseState.phase, phaseState]),
  );
  const processingPhase = phaseByCode.get("processing");
  const reconciliationPhase = phaseByCode.get("reconciliation");
  const commentaryComplete =
    latestReportRun !== null &&
    latestReportRun.status === "completed" &&
    latestReportRun.commentary.length > 0 &&
    latestReportRun.commentary.every((entry) => entry.status === "approved");

  return [
    {
      complete: processingPhase?.status === "completed",
      detail:
        processingPhase?.status === "completed"
          ? "All journal actions cleared."
          : "Journals still require posting or review.",
      label: "Accounting treatment complete",
      value: processingPhase?.status === "completed" ? "Ready" : "Open",
    },
    {
      complete: reconciliationPhase?.status === "completed",
      detail:
        reconciliationPhase?.status === "completed"
          ? "Reconciliation controls have passed."
          : "Exception queue still blocks release.",
      label: "Reconciliations complete",
      value: reconciliationPhase?.status === "completed" ? "Ready" : "Blocked",
    },
    {
      complete: commentaryComplete,
      detail: commentaryComplete
        ? "Management commentary is fully verified."
        : "Narrative review is still pending.",
      label: "Commentary finalized",
      value: commentaryComplete ? "Ready" : "Open",
    },
    {
      complete: latestEvidencePack !== null,
      detail:
        latestEvidencePack !== null
          ? `${latestEvidencePack.items.length} evidence item(s) bundled for audit coverage.`
          : "Evidence pack has not been assembled yet.",
      label: "Audit evidence coverage",
      value: latestEvidencePack !== null ? "100%" : "0%",
    },
  ];
}

function buildReleaseArtifactRows(options: {
  closeRunId: string;
  entityId: string;
  exportDetail: ExportDetail | null;
  generalLedgerExport: GeneralLedgerExportSummary | null;
  latestEvidencePack: EvidencePackBundle | null;
  latestReportRun: Awaited<ReturnType<typeof readReportRun>> | null;
}): readonly ReleaseArtifactRow[] {
  const rows: ReleaseArtifactRow[] = [];
  const seenKeys = new Set<string>();
  const reportArtifactDownloadMap = new Map<string, string>();

  if (options.latestReportRun !== null) {
    for (const artifactRef of options.latestReportRun.artifact_refs) {
      const artifactType = readArtifactString(artifactRef, "type", "");
      if (artifactType.length === 0) {
        continue;
      }
      reportArtifactDownloadMap.set(
        artifactType,
        buildReportArtifactDownloadPath(
          options.entityId,
          options.closeRunId,
          options.latestReportRun.id,
          artifactType,
        ),
      );
    }
  }

  if (options.generalLedgerExport !== null) {
    rows.push({
      actionHref: buildGeneralLedgerExportDownloadPath(options.entityId, options.closeRunId),
      actionLabel: "Download",
      filename: options.generalLedgerExport.filename,
      format: extractFileFormat(options.generalLedgerExport.filename),
      key: `general-ledger:${options.generalLedgerExport.idempotency_key}`,
      sourceLabel: "General ledger release artifact",
      statusLabel: "Generated",
      statusTone: "success",
    });
    seenKeys.add("general_ledger_export");
  }

  if (options.exportDetail !== null && options.exportDetail.manifest !== null) {
    for (const artifact of options.exportDetail.manifest.artifacts) {
      if (seenKeys.has(artifact.artifact_type)) {
        continue;
      }

      rows.push({
        actionHref: resolveExportArtifactDownloadHref(artifact, options, reportArtifactDownloadMap),
        actionLabel: resolveExportArtifactDownloadHref(artifact, options, reportArtifactDownloadMap)
          ? "Download"
          : null,
        filename: artifact.filename,
        format: extractFileFormat(artifact.filename),
        key: `${artifact.artifact_type}:${artifact.idempotency_key}`,
        sourceLabel: formatLabel(artifact.artifact_type),
        statusLabel:
          options.exportDetail.status === "completed"
            ? "Generated"
            : formatLabel(options.exportDetail.status),
        statusTone:
          options.exportDetail.status === "failed"
            ? "error"
            : options.exportDetail.status === "completed"
              ? "success"
              : "warning",
      });
      seenKeys.add(artifact.artifact_type);
    }
  }

  if (options.latestEvidencePack !== null && !seenKeys.has("evidence_pack")) {
    rows.push({
      actionHref: buildEvidencePackDownloadPath(options.entityId, options.closeRunId),
      actionLabel: "Download",
      filename: `Evidence Pack v${options.latestEvidencePack.version_no}.zip`,
      format: "ZIP",
      key: `evidence-pack:${options.latestEvidencePack.idempotency_key}`,
      sourceLabel: "Audit evidence bundle",
      statusLabel: "Generated",
      statusTone: "success",
    });
  }

  return rows;
}

function resolveExportArtifactDownloadHref(
  artifact: ExportArtifactEntry,
  options: {
    closeRunId: string;
    entityId: string;
    exportDetail: ExportDetail | null;
    generalLedgerExport: GeneralLedgerExportSummary | null;
    latestEvidencePack: EvidencePackBundle | null;
    latestReportRun: Awaited<ReturnType<typeof readReportRun>> | null;
  },
  reportArtifactDownloadMap: ReadonlyMap<string, string>,
): string | null {
  if (artifact.artifact_type === "evidence_pack" && options.latestEvidencePack !== null) {
    return buildEvidencePackDownloadPath(options.entityId, options.closeRunId);
  }

  if (artifact.artifact_type === "general_ledger_export" && options.generalLedgerExport !== null) {
    return buildGeneralLedgerExportDownloadPath(options.entityId, options.closeRunId);
  }

  return reportArtifactDownloadMap.get(artifact.artifact_type) ?? null;
}

function resolveReleaseStatusLabel(
  closeRun: Readonly<CloseRunSummary>,
  releaseReady: boolean,
): string {
  if (
    closeRun.status === "approved" ||
    closeRun.status === "exported" ||
    closeRun.status === "archived"
  ) {
    return `STATUS: ${formatLabel(closeRun.status).toUpperCase()}`;
  }

  return releaseReady ? "STATUS: READY FOR FINAL SIGN-OFF" : "STATUS: RELEASE PREPARATION OPEN";
}

function extractFileFormat(filename: string): string {
  const segments = filename.split(".");
  return segments.length > 1 ? segments.at(-1)!.toUpperCase() : "FILE";
}

function formatChannelLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function formatLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function formatTimestamp(value: string | null): string {
  if (value === null) {
    return "Pending";
  }

  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) {
    return value;
  }

  return parsed.toLocaleString("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatBytes(value: number): string {
  if (value <= 0) {
    return "0 bytes";
  }

  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  if (value >= 1024) {
    return `${Math.round(value / 1024)} KB`;
  }

  return `${value} bytes`;
}

function readArtifactString(
  artifactRef: Record<string, unknown>,
  key: string,
  fallback: string,
): string {
  const value = artifactRef[key];
  return typeof value === "string" && value.trim().length > 0 ? value : fallback;
}

function resolveExportErrorMessage(error: unknown): string {
  if (error instanceof ExportApiError) {
    return error.message;
  }
  if (error instanceof LedgerApiError) {
    return error.message;
  }
  if (error instanceof CloseRunApiError) {
    return error.message;
  }
  if (error instanceof ReportApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "The sign-off and release request failed.";
}
