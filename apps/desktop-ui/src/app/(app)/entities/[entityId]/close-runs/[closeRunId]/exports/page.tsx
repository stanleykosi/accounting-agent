"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import { use, useCallback, useEffect, useState, type ReactElement } from "react";
import {
  assembleEvidencePack,
  buildEvidencePackDownloadPath,
  distributeExport,
  type EvidencePackBundle,
  EXPORT_DELIVERY_CHANNELS,
  type ExportDetail,
  type ExportSummary,
  ExportApiError,
  listExports,
  readExportDetail,
  readLatestEvidencePack,
  triggerExport,
} from "../../../../../../../lib/exports";

type DistributionFormState = {
  deliveryChannel: string;
  note: string;
  recipientEmail: string;
  recipientName: string;
  recipientRole: string;
};

const defaultDistributionFormState: DistributionFormState = {
  deliveryChannel: "secure_email",
  note: "",
  recipientEmail: "",
  recipientName: "",
  recipientRole: "",
};

type CloseRunExportsPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

export default function CloseRunExportsPage({
  params,
}: Readonly<CloseRunExportsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [exportDetail, setExportDetail] = useState<ExportDetail | null>(null);
  const [exports, setExports] = useState<readonly ExportSummary[]>([]);
  const [isAssemblingEvidence, setIsAssemblingEvidence] = useState(false);
  const [isDistributingExport, setIsDistributingExport] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isTriggeringExport, setIsTriggeringExport] = useState(false);
  const [latestEvidencePack, setLatestEvidencePack] = useState<EvidencePackBundle | null>(null);
  const [selectedExportId, setSelectedExportId] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [distributionForm, setDistributionForm] = useState<DistributionFormState>(
    defaultDistributionFormState,
  );

  const loadExportsWorkspace = useCallback(async (preferredExportId?: string): Promise<void> => {
    setIsLoading(true);
    try {
      const [nextExports, nextEvidencePack] = await Promise.all([
        listExports(entityId, closeRunId),
        readLatestEvidencePack(entityId, closeRunId),
      ]);
      setExports(nextExports);
      setLatestEvidencePack(nextEvidencePack);
      const nextSelectedExportId = preferredExportId ?? selectedExportId ?? nextExports[0]?.id ?? null;
      setSelectedExportId(nextSelectedExportId);

      if (nextSelectedExportId !== null) {
        const detail = await readExportDetail(entityId, closeRunId, nextSelectedExportId);
        setExportDetail(detail);
      } else {
        setExportDetail(null);
      }

      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId, selectedExportId]);

  useEffect(() => {
    void loadExportsWorkspace();
  }, [loadExportsWorkspace]);

  async function handleTriggerExport(): Promise<void> {
    setIsTriggeringExport(true);
    try {
      const detail = await triggerExport(entityId, closeRunId);
      setStatusMessage(`Export created: ${detail.id}`);
      await loadExportsWorkspace(detail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsTriggeringExport(false);
    }
  }

  async function handleAssembleEvidencePack(): Promise<void> {
    setIsAssemblingEvidence(true);
    try {
      const evidencePack = await assembleEvidencePack(entityId, closeRunId);
      setLatestEvidencePack(evidencePack);
      setStatusMessage(`Evidence pack ready: ${evidencePack.idempotency_key}`);
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsAssemblingEvidence(false);
    }
  }

  async function handleSelectExport(exportId: string): Promise<void> {
    setSelectedExportId(exportId);
    try {
      const detail = await readExportDetail(entityId, closeRunId, exportId);
      setExportDetail(detail);
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    }
  }

  async function handleDistributeExport(): Promise<void> {
    if (exportDetail === null) {
      setErrorMessage("Select a completed export before recording management distribution.");
      return;
    }
    setIsDistributingExport(true);
    try {
      const detail = await distributeExport(entityId, closeRunId, exportDetail.id, {
        delivery_channel: distributionForm.deliveryChannel,
        note: distributionForm.note || null,
        recipient_email: distributionForm.recipientEmail,
        recipient_name: distributionForm.recipientName,
        recipient_role: distributionForm.recipientRole || null,
      });
      setExportDetail(detail);
      setStatusMessage(`Distribution recorded for ${distributionForm.recipientName}.`);
      setDistributionForm(defaultDistributionFormState);
      await loadExportsWorkspace(detail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveExportErrorMessage(error));
    } finally {
      setIsDistributingExport(false);
    }
  }

  return (
    <div className="app-shell close-run-exports-page">
      <section className="hero-grid close-run-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Release Center</p>
          <h1>Exports and evidence packs</h1>
          <p className="lede">
            Complete Step 10 of the accountant workflow: package the management distribution set,
            verify evidence coverage, and inspect the artifact manifest released for stakeholders.
          </p>
        </div>

        <SurfaceCard title="Release Actions" subtitle="Review / sign-off" tone="accent">
          <div className="integration-action-stack">
            <button
              className="primary-button"
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
              disabled={isAssemblingEvidence}
              onClick={() => {
                void handleAssembleEvidencePack();
              }}
              type="button"
            >
              {isAssemblingEvidence ? "Assembling..." : "Assemble evidence pack"}
            </button>
            <p className="form-helper">
              Evidence-pack assembly is idempotent, and exports snapshot the artifact manifest tied
              to this close-run version.
            </p>
          </div>
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

      <SurfaceCard title="Phase 5 Workflow Coverage" subtitle="Review, sign-off, and distribute">
        <div className="dashboard-row-list">
          <article className="dashboard-row">
            <strong className="close-run-row-title">Final review package</strong>
            <p className="form-helper">
              Exports and evidence packs are the controlled distribution outputs used for CFO or
              Finance Manager review, final adjustments, and management release.
            </p>
          </article>
          <article className="dashboard-row">
            <strong className="close-run-row-title">Distribution readiness</strong>
            <p className="form-helper">
              The close run cannot complete sign-off until a finished export, evidence pack, and
              recorded management distribution exist for the current version.
            </p>
          </article>
        </div>
      </SurfaceCard>

      <section className="content-grid">
        <SurfaceCard title="Exports" subtitle={`${exports.length} runs`}>
          {isLoading ? <p className="form-helper">Loading exports...</p> : null}
          {!isLoading && exports.length === 0 ? (
            <p className="form-helper">
              No export runs exist yet. Create the first export after reporting is complete.
            </p>
          ) : null}
          <div className="dashboard-row-list">
            {exports.map((exportRun) => (
              <article className="dashboard-row" key={exportRun.id}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">Export v{exportRun.version_no}</strong>
                    <p className="close-run-row-meta">
                      {exportRun.status.replaceAll("_", " ")} • {exportRun.artifact_count} artifacts
                    </p>
                  </div>
                </div>
                <p className="form-helper">
                  Created {formatTimestamp(exportRun.created_at)}
                  {exportRun.completed_at ? ` • Completed ${formatTimestamp(exportRun.completed_at)}` : ""}
                </p>
                <p className="form-helper">
                  Distributions recorded: {exportRun.distribution_count}
                  {exportRun.latest_distribution_at
                    ? ` • Latest ${formatTimestamp(exportRun.latest_distribution_at)}`
                    : ""}
                </p>
                <div className="close-run-link-row">
                  <button
                    className="secondary-button"
                    onClick={() => {
                      void handleSelectExport(exportRun.id);
                    }}
                    type="button"
                  >
                    Inspect export
                  </button>
                </div>
              </article>
            ))}
          </div>
        </SurfaceCard>

        <SurfaceCard
          title="Evidence Pack"
          subtitle={latestEvidencePack ? `Version ${latestEvidencePack.version_no}` : "Not assembled"}
        >
          {latestEvidencePack === null ? (
            <p className="form-helper">
              Assemble the evidence pack to package source references, extracted values, and
              released report outputs.
            </p>
          ) : (
            <>
              <dl className="entity-meta-grid close-run-summary-grid">
                <div>
                  <dt>Generated</dt>
                  <dd>{formatTimestamp(latestEvidencePack.generated_at)}</dd>
                </div>
                <div>
                  <dt>Storage key</dt>
                  <dd>{latestEvidencePack.storage_key ?? "Unavailable"}</dd>
                </div>
                <div>
                  <dt>Items</dt>
                  <dd>{latestEvidencePack.items.length}</dd>
                </div>
                <div>
                  <dt>Size</dt>
                  <dd>{latestEvidencePack.size_bytes ?? 0} bytes</dd>
                </div>
              </dl>
              <div className="close-run-link-row">
                <a
                  className="workspace-link-inline"
                  href={buildEvidencePackDownloadPath(entityId, closeRunId)}
                >
                  Download evidence pack
                </a>
              </div>
            </>
          )}
        </SurfaceCard>
      </section>

      <SurfaceCard
        title="Artifact Manifest"
        subtitle={exportDetail ? `Export ${exportDetail.id}` : "Select an export"}
      >
        {exportDetail === null ? (
          <p className="form-helper">Select an export to inspect its artifact manifest.</p>
        ) : exportDetail.manifest === null || exportDetail.manifest.artifacts.length === 0 ? (
          <p className="form-helper">No artifacts are attached to this export.</p>
        ) : (
          <div className="dashboard-row-list">
            {exportDetail.manifest.artifacts.map((artifact) => (
              <article className="dashboard-row" key={artifact.idempotency_key}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">{artifact.filename}</strong>
                    <p className="close-run-row-meta">
                      {artifact.artifact_type.replaceAll("_", " ")} • {artifact.content_type}
                    </p>
                  </div>
                </div>
                <p className="form-helper">
                  Storage key: {artifact.storage_key}
                </p>
                <p className="form-helper">
                  Released {formatTimestamp(artifact.released_at)} • {artifact.size_bytes} bytes
                </p>
              </article>
            ))}
          </div>
        )}
      </SurfaceCard>

      <section className="content-grid">
        <SurfaceCard
          title="Management Distribution"
          subtitle={exportDetail ? `Export ${exportDetail.version_no}` : "Select a completed export"}
        >
          {exportDetail === null ? (
            <p className="form-helper">
              Select an export to record CFO, Finance Manager, or management distribution.
            </p>
          ) : (
            <div className="integration-action-stack">
              <label className="form-field">
                <span className="form-label">Recipient name</span>
                <input
                  className="text-input"
                  onChange={(event) => {
                    setDistributionForm((current) => ({
                      ...current,
                      recipientName: event.target.value,
                    }));
                  }}
                  placeholder="Amina Yusuf"
                  type="text"
                  value={distributionForm.recipientName}
                />
              </label>
              <label className="form-field">
                <span className="form-label">Recipient email</span>
                <input
                  className="text-input"
                  onChange={(event) => {
                    setDistributionForm((current) => ({
                      ...current,
                      recipientEmail: event.target.value,
                    }));
                  }}
                  placeholder="cfo@example.com"
                  type="email"
                  value={distributionForm.recipientEmail}
                />
              </label>
              <label className="form-field">
                <span className="form-label">Role</span>
                <input
                  className="text-input"
                  onChange={(event) => {
                    setDistributionForm((current) => ({
                      ...current,
                      recipientRole: event.target.value,
                    }));
                  }}
                  placeholder="Chief Financial Officer"
                  type="text"
                  value={distributionForm.recipientRole}
                />
              </label>
              <label className="form-field">
                <span className="form-label">Delivery channel</span>
                <select
                  className="text-input"
                  onChange={(event) => {
                    setDistributionForm((current) => ({
                      ...current,
                      deliveryChannel: event.target.value,
                    }));
                  }}
                  value={distributionForm.deliveryChannel}
                >
                  {EXPORT_DELIVERY_CHANNELS.map((channel) => (
                    <option key={channel} value={channel}>
                      {formatChannelLabel(channel)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="form-field">
                <span className="form-label">Operator note</span>
                <textarea
                  className="text-input form-textarea"
                  onChange={(event) => {
                    setDistributionForm((current) => ({
                      ...current,
                      note: event.target.value,
                    }));
                  }}
                  placeholder="Board pack shared after final variance review."
                  rows={4}
                  value={distributionForm.note}
                />
              </label>
              <button
                className="primary-button"
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
              <p className="form-helper">
                Step 10 stays blocked until the final export package has a named management
                distribution record.
              </p>
              {exportDetail.status !== "completed" ? (
                <p className="form-helper">
                  Complete export generation first. Distribution can only be recorded against a
                  completed export package.
                </p>
              ) : null}
            </div>
          )}
        </SurfaceCard>

        <SurfaceCard
          title="Distribution History"
          subtitle={
            exportDetail
              ? `${exportDetail.distribution_records.length} stakeholder releases`
              : "No export selected"
          }
        >
          {exportDetail === null ? (
            <p className="form-helper">Select an export to inspect its stakeholder release log.</p>
          ) : exportDetail.distribution_records.length === 0 ? (
            <p className="form-helper">
              No management distribution has been recorded yet for this export.
            </p>
          ) : (
            <div className="dashboard-row-list">
              {exportDetail.distribution_records.map((record) => (
                <article className="dashboard-row" key={record.id}>
                  <div className="close-run-row-header">
                    <div>
                      <strong className="close-run-row-title">{record.recipient_name}</strong>
                      <p className="close-run-row-meta">
                        {record.recipient_role ?? "Management recipient"} •{" "}
                        {formatChannelLabel(record.delivery_channel)}
                      </p>
                    </div>
                  </div>
                  <p className="form-helper">
                    {record.recipient_email} • Distributed {formatTimestamp(record.distributed_at)}
                  </p>
                  {record.note ? <p className="form-helper">{record.note}</p> : null}
                </article>
              ))}
            </div>
          )}
        </SurfaceCard>
      </section>
    </div>
  );
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

function resolveExportErrorMessage(error: unknown): string {
  if (error instanceof ExportApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "The export request failed.";
}

function formatChannelLabel(value: string): string {
  return value.replaceAll("_", " ");
}
