"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../../../../../components/layout/QuartzIcons";
import {
  archiveCloseRun,
  CloseRunApiError,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  readCloseRunWorkspace,
  type CloseRunWorkspaceData,
} from "../../../../../../../lib/close-runs";
import {
  buildEvidencePackDownloadPath,
  type EvidencePackBundle,
  type ExportArtifactEntry,
  type ExportDetail,
  ExportApiError,
  listExports,
  readExportDetail,
  readLatestEvidencePack,
} from "../../../../../../../lib/exports";
import {
  buildGeneralLedgerExportDownloadPath,
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
import { requireRouteParam } from "../../../../../../../lib/route-params";

type CompletionWorkspaceData = {
  closeRunWorkspace: CloseRunWorkspaceData;
  exportDetail: ExportDetail | null;
  latestEvidencePack: EvidencePackBundle | null;
  latestGeneralLedgerExport: GeneralLedgerExportSummary | null;
  latestReportRun: Awaited<ReturnType<typeof readReportRun>> | null;
};

type CompletionStatusCard = Readonly<{
  label: string;
  tone: "error" | "neutral" | "success" | "warning";
  value: string;
}>;

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

export default function CloseRunCompletePage(): ReactElement {
  const routeParams = useParams<{ closeRunId: string; entityId: string }>();
  const closeRunId = requireRouteParam(routeParams.closeRunId, "closeRunId");
  const entityId = requireRouteParam(routeParams.entityId, "entityId");

  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isArchiving, setIsArchiving] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<CompletionWorkspaceData | null>(null);

  const loadCompletionWorkspace = useCallback(async (): Promise<void> => {
    setIsLoading(true);
    try {
      const [
        closeRunWorkspace,
        exports,
        latestEvidencePack,
        latestGeneralLedgerExport,
        reportRuns,
      ] = await Promise.all([
        readCloseRunWorkspace(entityId, closeRunId),
        listExports(entityId, closeRunId),
        readLatestEvidencePack(entityId, closeRunId),
        readLatestGeneralLedgerExport(entityId, closeRunId),
        listReportRuns(entityId, closeRunId),
      ]);

      const latestExportId = exports[0]?.id ?? null;
      const latestReportRunId = reportRuns[0]?.id ?? null;
      const [exportDetail, latestReportRun] = await Promise.all([
        latestExportId === null
          ? Promise.resolve(null)
          : readExportDetail(entityId, closeRunId, latestExportId),
        latestReportRunId === null
          ? Promise.resolve(null)
          : readReportRun(entityId, closeRunId, latestReportRunId),
      ]);

      setWorkspaceData({
        closeRunWorkspace,
        exportDetail,
        latestEvidencePack,
        latestGeneralLedgerExport,
        latestReportRun,
      });
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveCompletionErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId]);

  useEffect(() => {
    void loadCompletionWorkspace();
  }, [loadCompletionWorkspace]);

  const completionState = useMemo(() => {
    if (workspaceData === null) {
      return null;
    }

    return deriveCompletionState(workspaceData);
  }, [workspaceData]);

  async function handleArchiveCloseRun(): Promise<void> {
    if (workspaceData === null) {
      return;
    }

    setIsArchiving(true);
    setStatusMessage(null);
    try {
      await archiveCloseRun(entityId, closeRunId, "Archived from close complete workspace");
      setStatusMessage("Close run archived.");
      await loadCompletionWorkspace();
    } catch (error: unknown) {
      setErrorMessage(resolveCompletionErrorMessage(error));
    } finally {
      setIsArchiving(false);
    }
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading close completion...</div>
        </section>
      </div>
    );
  }

  if (workspaceData === null || completionState === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The close completion workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  const { closeRunWorkspace, exportDetail, latestEvidencePack } = workspaceData;
  const closeRun = closeRunWorkspace.closeRun;
  const entityName = closeRunWorkspace.entity.name;
  const {
    approvalTimestamp,
    auditNote,
    finalDownloadHref,
    isComplete,
    manifestRows,
    releaseStatusCards,
    title,
  } = completionState;

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel quartz-complete-page">
        <header className="quartz-complete-header">
          <div className="quartz-complete-badge">
            <QuartzIcon className="quartz-complete-badge-icon" name="check" />
          </div>
          <h1>{title}</h1>
          <p className="quartz-page-subtitle">
            {isComplete
              ? "The financial period has been closed, approved, and released to the ledger."
              : "Final release is still settling. Review the release markers below before treating the period as complete."}
          </p>
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

        {!isComplete ? (
          <div className="status-banner warning quartz-section" role="status">
            This close run is not yet in an approved, exported, or archived state. Return to{" "}
            <Link href={`/entities/${entityId}/close-runs/${closeRunId}/exports`}>
              Sign-Off and Release
            </Link>{" "}
            to finish the governed release actions.
          </div>
        ) : null}

        <section className="quartz-section quartz-complete-summary-grid">
          <article className="quartz-card quartz-complete-summary-card">
            <div className="quartz-section-header quartz-section-header-tight">
              <h2 className="quartz-section-title">Close Summary</h2>
            </div>
            <div className="quartz-summary-list">
              <div className="quartz-summary-row">
                <span className="quartz-table-secondary">Entity</span>
                <strong>{entityName}</strong>
              </div>
              <div className="quartz-summary-row">
                <span className="quartz-table-secondary">Period</span>
                <strong>{formatCloseRunPeriod(closeRun)}</strong>
              </div>
              <div className="quartz-summary-row">
                <span className="quartz-table-secondary">Currency</span>
                <strong>{closeRun.reportingCurrency}</strong>
              </div>
              <div className="quartz-summary-row">
                <span className="quartz-table-secondary">Status</span>
                <strong className="quartz-complete-status-text">
                  <QuartzIcon className="quartz-inline-icon" name="check" />
                  {isComplete ? "Fully approved" : getCloseRunStatusLabel(closeRun.status)}
                </strong>
              </div>
              <div className="quartz-summary-row">
                <span className="quartz-table-secondary">Version</span>
                <strong className="quartz-table-numeric">v{closeRun.currentVersionNo}</strong>
              </div>
              <div className="quartz-summary-row">
                <span className="quartz-table-secondary">Timestamp</span>
                <strong>{approvalTimestamp}</strong>
              </div>
            </div>
          </article>

          <div className="quartz-complete-status-cards">
            {releaseStatusCards.map((card) => (
              <article className="quartz-card quartz-complete-status-card" key={card.label}>
                <span className={`quartz-status-badge ${card.tone}`}>{card.label}</span>
                <strong>{card.value}</strong>
              </article>
            ))}
          </div>
        </section>

        <section className="quartz-section">
          <article className="quartz-card quartz-card-table-shell">
            <div className="quartz-section-header">
              <h2 className="quartz-section-title">Final Outputs Manifest</h2>
              {exportDetail ? (
                <Link
                  className="quartz-filter-link"
                  href={`/entities/${entityId}/close-runs/${closeRunId}/exports`}
                >
                  View signed-off close
                </Link>
              ) : null}
            </div>

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
                {manifestRows.length === 0 ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="quartz-empty-state">
                        Released artifacts will appear here once the export package and evidence
                        bundle are complete.
                      </div>
                    </td>
                  </tr>
                ) : (
                  manifestRows.map((row) => (
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
                      <td className="quartz-table-center">
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
          </article>
        </section>

        <section className="quartz-section quartz-complete-actions">
          <div className="quartz-button-row quartz-complete-primary-action">
            <Link className="primary-button" href="/">
              Return to Command Center
            </Link>
          </div>

          <div className="quartz-inline-action-row quartz-complete-secondary-actions">
            <Link
              className="secondary-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/chat`}
            >
              Open Assistant
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/exports`}
            >
              View Signed-Off Close
            </Link>
            {finalDownloadHref ? (
              <a className="secondary-button" href={finalDownloadHref}>
                Download Final Release Package
              </a>
            ) : null}
            <button
              className="secondary-button"
              disabled={isArchiving || closeRun.status === "archived"}
              onClick={() => {
                void handleArchiveCloseRun();
              }}
              type="button"
            >
              {closeRun.status === "archived"
                ? "Close Run Archived"
                : isArchiving
                  ? "Archiving..."
                  : "Archive Close Run"}
            </button>
            <Link className="secondary-button" href={`/entities/${entityId}#new-close-run`}>
              Begin Next Period Preparation
            </Link>
          </div>

          <article className="quartz-card quartz-complete-audit-note">
            <QuartzIcon className="quartz-inline-icon" name="close" />
            <p>
              <strong>Audit Trail Finalized:</strong> {auditNote}
            </p>
          </article>
        </section>

        {latestEvidencePack ? (
          <section className="quartz-section">
            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-section-title">Evidence Coverage</h2>
                <span className="quartz-status-badge success">Verified</span>
              </div>
              <div className="quartz-summary-list">
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Pack version</span>
                  <strong>v{latestEvidencePack.version_no}</strong>
                </div>
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Items bundled</span>
                  <strong>{latestEvidencePack.items.length}</strong>
                </div>
                <div className="quartz-summary-row">
                  <span className="quartz-table-secondary">Generated</span>
                  <strong>{formatTimestamp(latestEvidencePack.generated_at)}</strong>
                </div>
              </div>
            </article>
          </section>
        ) : null}
      </section>
    </div>
  );
}

function deriveCompletionState(workspaceData: Readonly<CompletionWorkspaceData>): Readonly<{
  approvalTimestamp: string;
  auditNote: string;
  finalDownloadHref: string | null;
  isComplete: boolean;
  manifestRows: readonly ReleaseArtifactRow[];
  releaseStatusCards: readonly CompletionStatusCard[];
  title: string;
}> {
  const closeRun = workspaceData.closeRunWorkspace.closeRun;
  const isComplete =
    closeRun.status === "approved" ||
    closeRun.status === "exported" ||
    closeRun.status === "archived";
  const manifestRows = buildReleaseArtifactRows({
    closeRunId: closeRun.id,
    entityId: closeRun.entityId,
    exportDetail: workspaceData.exportDetail,
    generalLedgerExport: workspaceData.latestGeneralLedgerExport,
    latestEvidencePack: workspaceData.latestEvidencePack,
    latestReportRun: workspaceData.latestReportRun,
  });
  const finalDownloadHref =
    workspaceData.latestEvidencePack !== null
      ? buildEvidencePackDownloadPath(closeRun.entityId, closeRun.id)
      : (manifestRows.find((row) => row.actionHref !== null)?.actionHref ?? null);

  return {
    approvalTimestamp: formatTimestamp(closeRun.approvedAt ?? closeRun.updatedAt),
    auditNote:
      workspaceData.latestEvidencePack !== null
        ? "This close run is evidence-backed, fully versioned, and locked for downstream review."
        : "This close run is versioned and approved. Assemble the evidence pack to finalize audit packaging.",
    finalDownloadHref,
    isComplete,
    manifestRows,
    releaseStatusCards: buildCompletionStatusCards(workspaceData),
    title: isComplete
      ? `${formatPeriodForHeadline(closeRun.periodEnd)} Close Fully Completed`
      : `${formatPeriodForHeadline(closeRun.periodEnd)} Close Near Completion`,
  };
}

function buildCompletionStatusCards(
  workspaceData: Readonly<CompletionWorkspaceData>,
): readonly CompletionStatusCard[] {
  const distributionCount = workspaceData.exportDetail?.distribution_records.length ?? 0;
  return [
    {
      label: "Export package",
      tone:
        workspaceData.exportDetail === null
          ? "warning"
          : workspaceData.exportDetail.status === "completed"
            ? "success"
            : workspaceData.exportDetail.status === "failed"
              ? "error"
              : "warning",
      value:
        workspaceData.exportDetail === null
          ? "Pending"
          : workspaceData.exportDetail.status === "completed"
            ? "Generated"
            : formatLabel(workspaceData.exportDetail.status),
    },
    {
      label: "Evidence pack",
      tone: workspaceData.latestEvidencePack === null ? "warning" : "success",
      value: workspaceData.latestEvidencePack === null ? "Pending" : "Verified",
    },
    {
      label: "Distribution",
      tone: distributionCount > 0 ? "success" : "warning",
      value: distributionCount > 0 ? `Recorded (${distributionCount})` : "Not recorded",
    },
    {
      label: "GL export",
      tone: workspaceData.latestGeneralLedgerExport === null ? "warning" : "success",
      value: workspaceData.latestGeneralLedgerExport === null ? "Pending" : "Committed",
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

      const actionHref = resolveExportArtifactDownloadHref(
        artifact,
        options,
        reportArtifactDownloadMap,
      );
      rows.push({
        actionHref,
        actionLabel: actionHref ? "Download" : null,
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
      statusLabel: "Released",
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

function extractFileFormat(filename: string): string {
  const segments = filename.split(".");
  return segments.length > 1 ? segments.at(-1)!.toUpperCase() : "FILE";
}

function formatPeriodForHeadline(periodEnd: string): string {
  const parsed = new Date(periodEnd);
  if (Number.isNaN(parsed.valueOf())) {
    return "Period";
  }

  return parsed.toLocaleDateString("en-NG", {
    month: "long",
    year: "numeric",
  });
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

function readArtifactString(
  artifactRef: Record<string, unknown>,
  key: string,
  fallback: string,
): string {
  const value = artifactRef[key];
  return typeof value === "string" && value.trim().length > 0 ? value : fallback;
}

function resolveCompletionErrorMessage(error: unknown): string {
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
  return "The close completion request failed.";
}
