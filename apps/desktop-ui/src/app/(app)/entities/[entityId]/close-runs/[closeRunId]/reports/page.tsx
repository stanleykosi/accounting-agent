"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import { use, useCallback, useEffect, useState, type ReactElement } from "react";
import {
  approveReportCommentary,
  buildReportArtifactDownloadPath,
  generateReportRun,
  listReportRuns,
  readReportRun,
  ReportApiError,
  type CommentarySummary,
  type ReportRunSummary,
  updateReportCommentary,
} from "../../../../../../../lib/reports";

type CloseRunReportsPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

type ReportRunDetailRecord = Awaited<ReturnType<typeof readReportRun>>;

export default function CloseRunReportsPage({
  params,
}: Readonly<CloseRunReportsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSavingSectionKey, setIsSavingSectionKey] = useState<string | null>(null);
  const [reportRuns, setReportRuns] = useState<readonly ReportRunSummary[]>([]);
  const [reportRunDetail, setReportRunDetail] = useState<ReportRunDetailRecord | null>(null);
  const [selectedReportRunId, setSelectedReportRunId] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [draftCommentary, setDraftCommentary] = useState<Record<string, string>>({});

  const loadReportWorkspace = useCallback(async (preferredReportRunId?: string): Promise<void> => {
    setIsLoading(true);
    try {
      const runs = await listReportRuns(entityId, closeRunId);
      setReportRuns(runs);
      const nextSelectedId =
        preferredReportRunId ??
        selectedReportRunId ??
        runs[0]?.id ??
        null;
      setSelectedReportRunId(nextSelectedId);

      if (nextSelectedId !== null) {
        const detail = await readReportRun(entityId, closeRunId, nextSelectedId);
        setReportRunDetail(detail);
        setDraftCommentary(
          Object.fromEntries(
            detail.commentary.map((entry) => [entry.section_key, entry.body]),
          ),
        );
      } else {
        setReportRunDetail(null);
        setDraftCommentary({});
      }

      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    } finally {
      setIsLoading(false);
    }
  }, [closeRunId, entityId, selectedReportRunId]);

  useEffect(() => {
    void loadReportWorkspace();
  }, [loadReportWorkspace]);

  async function handleGenerateReportRun(): Promise<void> {
    setIsGenerating(true);
    try {
      const run = await generateReportRun(entityId, closeRunId, {
        generateCommentary: true,
        useLlmCommentary: false,
      });
      setStatusMessage(`Report run queued: v${run.version_no}`);
      await loadReportWorkspace(run.id);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleSelectRun(reportRunId: string): Promise<void> {
    setSelectedReportRunId(reportRunId);
    try {
      const detail = await readReportRun(entityId, closeRunId, reportRunId);
      setReportRunDetail(detail);
      setDraftCommentary(
        Object.fromEntries(detail.commentary.map((entry) => [entry.section_key, entry.body])),
      );
      setErrorMessage(null);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    }
  }

  async function handleSaveCommentary(sectionKey: string): Promise<void> {
    const body = draftCommentary[sectionKey]?.trim() ?? "";
    if (body.length === 0 || reportRunDetail === null) {
      return;
    }

    setIsSavingSectionKey(sectionKey);
    try {
      await updateReportCommentary(entityId, closeRunId, reportRunDetail.id, sectionKey, body);
      setStatusMessage(`Saved commentary for ${formatSectionLabel(sectionKey)}.`);
      await loadReportWorkspace(reportRunDetail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    } finally {
      setIsSavingSectionKey(null);
    }
  }

  async function handleApproveCommentary(sectionKey: string): Promise<void> {
    const body = draftCommentary[sectionKey]?.trim() ?? null;
    if (reportRunDetail === null) {
      return;
    }

    setIsSavingSectionKey(sectionKey);
    try {
      await approveReportCommentary(
        entityId,
        closeRunId,
        reportRunDetail.id,
        sectionKey,
        body,
        "Approved in reporting workspace",
      );
      setStatusMessage(`Approved commentary for ${formatSectionLabel(sectionKey)}.`);
      await loadReportWorkspace(reportRunDetail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    } finally {
      setIsSavingSectionKey(null);
    }
  }

  return (
    <div className="app-shell close-run-reports-page">
      <section className="hero-grid close-run-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Reporting</p>
          <h1>Report runs and management commentary</h1>
          <p className="lede">
            Generate reporting packs for this close run, review the produced artifact set, and
            finalize commentary before sign-off and export.
          </p>
        </div>

        <SurfaceCard title="Reporting Actions" subtitle="Reporting phase" tone="accent">
          <div className="integration-action-stack">
            <button
              className="primary-button"
              disabled={isGenerating}
              onClick={() => {
                void handleGenerateReportRun();
              }}
              type="button"
            >
              {isGenerating ? "Queueing..." : "Generate report pack"}
            </button>
            <p className="form-helper">
              Each run snapshots the current close-run state and commentary for auditability.
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

      <SurfaceCard title="Phase 4 Workflow Coverage" subtitle="Steps 08 and 09">
        <div className="dashboard-row-list">
          <article className="dashboard-row">
            <strong className="close-run-row-title">08. Prepare management report</strong>
            <p className="form-helper">
              Each report run produces the management-report artifact set: P&amp;L, Balance
              Sheet, Cash Flow, budget variance analysis, and KPI dashboard outputs.
            </p>
          </article>
          <article className="dashboard-row">
            <strong className="close-run-row-title">09. Write commentary and analysis</strong>
            <p className="form-helper">
              Commentary drafts, edits, and approvals live below so finance reviewers can explain
              variances, risks, highlights, and management actions before sign-off.
            </p>
          </article>
        </div>
      </SurfaceCard>

      <section className="content-grid">
        <SurfaceCard title="Report Runs" subtitle={`${reportRuns.length} runs`}>
          {isLoading ? <p className="form-helper">Loading report runs...</p> : null}
          {!isLoading && reportRuns.length === 0 ? (
            <p className="form-helper">
              No report runs exist yet. Generate the first run to create Excel/PDF outputs and
              draft commentary.
            </p>
          ) : null}
          <div className="dashboard-row-list">
            {reportRuns.map((run) => (
              <article className="dashboard-row" key={run.id}>
                <div className="close-run-row-header">
                  <div>
                    <strong className="close-run-row-title">Run v{run.version_no}</strong>
                    <p className="close-run-row-meta">
                      {run.status.replaceAll("_", " ")} • Template {run.template_id}
                    </p>
                  </div>
                </div>
                <p className="form-helper">
                  Created {formatTimestamp(run.created_at)}
                  {run.completed_at ? ` • Completed ${formatTimestamp(run.completed_at)}` : ""}
                </p>
                <div className="close-run-link-row">
                  <button
                    className="secondary-button"
                    onClick={() => {
                      void handleSelectRun(run.id);
                    }}
                    type="button"
                  >
                    Inspect run
                  </button>
                </div>
              </article>
            ))}
          </div>
        </SurfaceCard>

        <SurfaceCard
          title="Artifacts"
          subtitle={reportRunDetail ? `Run v${reportRunDetail.version_no}` : "Select a run"}
        >
          {reportRunDetail === null ? (
            <p className="form-helper">Select a report run to inspect generated artifacts.</p>
          ) : reportRunDetail.artifact_refs.length === 0 ? (
            <p className="form-helper">
              No artifacts are attached yet. This run may still be processing or may have failed.
            </p>
          ) : (
            <div className="dashboard-row-list">
              {reportRunDetail.artifact_refs.map((artifactRef, index) => (
                <ReportArtifactRow
                  artifactRef={artifactRef}
                  closeRunId={closeRunId}
                  entityId={entityId}
                  index={index}
                  key={`${reportRunDetail.id}-artifact-${index}`}
                  reportRunId={reportRunDetail.id}
                />
              ))}
            </div>
          )}
        </SurfaceCard>
      </section>

      <SurfaceCard
        title="Management Commentary"
        subtitle={reportRunDetail ? `Run v${reportRunDetail.version_no}` : "Select a run"}
      >
        {reportRunDetail === null ? (
          <p className="form-helper">Select a run to review or approve commentary.</p>
        ) : reportRunDetail.commentary.length === 0 ? (
          <p className="form-helper">
            Commentary has not been generated for this run yet.
          </p>
        ) : (
          <div className="dashboard-row-list">
            {reportRunDetail.commentary.map((entry) => (
              <CommentaryEditor
                key={entry.id}
                commentary={entry}
                disabled={isSavingSectionKey === entry.section_key}
                value={draftCommentary[entry.section_key] ?? entry.body}
                onApprove={() => {
                  void handleApproveCommentary(entry.section_key);
                }}
                onChange={(nextValue) => {
                  setDraftCommentary((current) => ({
                    ...current,
                    [entry.section_key]: nextValue,
                  }));
                }}
                onSave={() => {
                  void handleSaveCommentary(entry.section_key);
                }}
              />
            ))}
          </div>
        )}
      </SurfaceCard>
    </div>
  );
}

type ReportArtifactRowProps = {
  artifactRef: Record<string, unknown>;
  closeRunId: string;
  entityId: string;
  index: number;
  reportRunId: string;
};

function ReportArtifactRow({
  artifactRef,
  closeRunId,
  entityId,
  index,
  reportRunId,
}: Readonly<ReportArtifactRowProps>): ReactElement {
  const artifactType = readArtifactString(artifactRef, "type", "artifact");
  const artifactFilename = readArtifactString(artifactRef, "filename", `Artifact ${index + 1}`);
  const artifactSizeBytes = readArtifactNumber(artifactRef, "size_bytes");
  const storageKey = readArtifactString(artifactRef, "storage_key", "Unavailable");

  return (
    <article className="dashboard-row">
      <div className="close-run-row-header">
        <div>
          <strong className="close-run-row-title">{artifactFilename}</strong>
          <p className="close-run-row-meta">
            {artifactType} • {artifactSizeBytes} bytes
          </p>
        </div>
      </div>
      <p className="form-helper">Storage key: {storageKey}</p>
      <div className="close-run-link-row">
        <a
          className="workspace-link-inline"
          href={buildReportArtifactDownloadPath(entityId, closeRunId, reportRunId, artifactType)}
        >
          Download
        </a>
      </div>
    </article>
  );
}

type CommentaryEditorProps = {
  commentary: CommentarySummary;
  disabled: boolean;
  value: string;
  onApprove: () => void;
  onChange: (value: string) => void;
  onSave: () => void;
};

function CommentaryEditor({
  commentary,
  disabled,
  value,
  onApprove,
  onChange,
  onSave,
}: Readonly<CommentaryEditorProps>): ReactElement {
  return (
    <article className="dashboard-row">
      <div className="close-run-row-header">
        <div>
          <strong className="close-run-row-title">{formatSectionLabel(commentary.section_key)}</strong>
          <p className="close-run-row-meta">{commentary.status.replaceAll("_", " ")}</p>
        </div>
      </div>
      <textarea
        className="form-textarea"
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        rows={6}
        value={value}
      />
      <div className="close-run-link-row">
        <button className="secondary-button" disabled={disabled} onClick={onSave} type="button">
          {disabled ? "Saving..." : "Save draft"}
        </button>
        <button className="secondary-button" disabled={disabled} onClick={onApprove} type="button">
          {disabled ? "Saving..." : "Approve"}
        </button>
      </div>
    </article>
  );
}

function formatSectionLabel(sectionKey: string): string {
  return sectionKey.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
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

function readArtifactNumber(artifactRef: Record<string, unknown>, key: string): number {
  const value = artifactRef[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number(value);
    if (!Number.isNaN(parsed)) {
      return parsed;
    }
  }
  return 0;
}

function resolveReportErrorMessage(error: unknown): string {
  if (error instanceof ReportApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "The reporting workspace request failed.";
}
