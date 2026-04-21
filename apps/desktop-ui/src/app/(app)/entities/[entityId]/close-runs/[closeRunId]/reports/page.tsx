"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../../../../../components/layout/QuartzIcons";
import {
  CloseRunApiError,
  formatCloseRunPeriod,
  readCloseRunWorkspace,
  type CloseRunWorkspaceData,
} from "../../../../../../../lib/close-runs";
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

type SelectedArtifact = Readonly<{
  artifactRef: Record<string, unknown>;
  artifactType: string;
  filename: string;
  key: string;
  sizeBytes: number;
  storageKey: string;
}>;

const emptyArtifacts: readonly Record<string, unknown>[] = [];
const emptyCommentary: readonly CommentarySummary[] = [];
const emptyReportRuns: readonly ReportRunSummary[] = [];

export default function CloseRunReportsPage({
  params,
}: Readonly<CloseRunReportsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);

  const [closeRunWorkspace, setCloseRunWorkspace] = useState<CloseRunWorkspaceData | null>(null);
  const [draftCommentary, setDraftCommentary] = useState<Record<string, string>>({});
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSavingSectionKey, setIsSavingSectionKey] = useState<string | null>(null);
  const [reportRunDetail, setReportRunDetail] = useState<ReportRunDetailRecord | null>(null);
  const [reportRuns, setReportRuns] = useState<readonly ReportRunSummary[]>(emptyReportRuns);
  const [selectedArtifactKey, setSelectedArtifactKey] = useState<string | null>(null);
  const [selectedReportRunId, setSelectedReportRunId] = useState<string | null>(null);
  const [selectedSectionKey, setSelectedSectionKey] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const loadReportWorkspace = useCallback(
    async (preferredReportRunId?: string): Promise<void> => {
      setIsLoading(true);
      try {
        const [nextCloseRunWorkspace, nextRuns] = await Promise.all([
          readCloseRunWorkspace(entityId, closeRunId),
          listReportRuns(entityId, closeRunId),
        ]);

        setCloseRunWorkspace(nextCloseRunWorkspace);
        setReportRuns(nextRuns);

        const nextSelectedId =
          preferredReportRunId ?? selectedReportRunId ?? nextRuns[0]?.id ?? null;
        setSelectedReportRunId(nextSelectedId);

        if (nextSelectedId !== null) {
          const detail = await readReportRun(entityId, closeRunId, nextSelectedId);
          setReportRunDetail(detail);
          setDraftCommentary(
            Object.fromEntries(detail.commentary.map((entry) => [entry.section_key, entry.body])),
          );
          setSelectedSectionKey((currentSelectedSectionKey) =>
            detail.commentary.some((entry) => entry.section_key === currentSelectedSectionKey)
              ? currentSelectedSectionKey
              : (detail.commentary[0]?.section_key ?? null),
          );
          setSelectedArtifactKey((currentSelectedArtifactKey) => {
            const artifactKeys = detail.artifact_refs.map((artifactRef, index) =>
              getArtifactSelectionKey(artifactRef, index),
            );
            return artifactKeys.includes(currentSelectedArtifactKey ?? "")
              ? currentSelectedArtifactKey
              : (artifactKeys[0] ?? null);
          });
        } else {
          setReportRunDetail(null);
          setDraftCommentary({});
          setSelectedSectionKey(null);
          setSelectedArtifactKey(null);
        }

        setErrorMessage(null);
      } catch (error: unknown) {
        setErrorMessage(resolveReportErrorMessage(error));
      } finally {
        setIsLoading(false);
      }
    },
    [closeRunId, entityId, selectedReportRunId],
  );

  useEffect(() => {
    void loadReportWorkspace();
  }, [loadReportWorkspace]);

  const commentaryEntries = reportRunDetail?.commentary ?? emptyCommentary;
  const artifactRefs = reportRunDetail?.artifact_refs ?? emptyArtifacts;
  const selectedCommentary =
    commentaryEntries.find((entry) => entry.section_key === selectedSectionKey) ??
    commentaryEntries[0] ??
    null;
  const selectedArtifact = useMemo(
    () => deriveSelectedArtifact(artifactRefs, selectedArtifactKey),
    [artifactRefs, selectedArtifactKey],
  );
  const selectedArtifactHref =
    reportRunDetail !== null && selectedArtifact !== null
      ? buildReportArtifactDownloadPath(
          entityId,
          closeRunId,
          reportRunDetail.id,
          selectedArtifact.artifactType,
        )
      : null;
  const pendingCommentary = useMemo(
    () => commentaryEntries.filter((entry) => entry.status !== "approved"),
    [commentaryEntries],
  );
  const readinessPercent = deriveReportingReadiness(reportRunDetail);

  async function handleGenerateReportRun(): Promise<void> {
    setIsGenerating(true);
    setStatusMessage(null);
    try {
      const run = await generateReportRun(entityId, closeRunId, {
        generateCommentary: true,
        useLlmCommentary: false,
      });
      setStatusMessage(`Report run queued: v${run.version_no}.`);
      await loadReportWorkspace(run.id);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    } finally {
      setIsGenerating(false);
    }
  }

  async function handleSelectRun(reportRunId: string): Promise<void> {
    setStatusMessage(null);
    await loadReportWorkspace(reportRunId);
  }

  async function handleSaveCommentary(sectionKey: string): Promise<void> {
    const body = draftCommentary[sectionKey]?.trim() ?? "";
    if (body.length === 0 || reportRunDetail === null) {
      return;
    }

    setIsSavingSectionKey(sectionKey);
    setStatusMessage(null);
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
    if (reportRunDetail === null) {
      return;
    }

    setIsSavingSectionKey(sectionKey);
    setStatusMessage(null);
    try {
      await approveReportCommentary(
        entityId,
        closeRunId,
        reportRunDetail.id,
        sectionKey,
        draftCommentary[sectionKey]?.trim() ?? null,
        "Approved in reporting workspace",
      );
      setStatusMessage(`Marked ${formatSectionLabel(sectionKey)} as verified.`);
      await loadReportWorkspace(reportRunDetail.id);
    } catch (error: unknown) {
      setErrorMessage(resolveReportErrorMessage(error));
    } finally {
      setIsSavingSectionKey(null);
    }
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading reporting and commentary...</div>
        </section>
      </div>
    );
  }

  if (closeRunWorkspace === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The reporting workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  const closeRun = closeRunWorkspace.closeRun;
  const entityName = closeRunWorkspace.entity.name;

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <p className="quartz-kpi-label">
              {entityName} • {formatCloseRunPeriod(closeRun)}
            </p>
            <h1>Reporting and Commentary</h1>
            <div className="quartz-header-stat-row">
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Reporting readiness</span>
                <span className="quartz-header-stat-value">{readinessPercent}%</span>
              </div>
              <div className="quartz-header-stat">
                <span className="quartz-kpi-label">Narrative review</span>
                <span
                  className={`quartz-header-stat-value ${
                    pendingCommentary.length > 0 ? "warning" : "success"
                  }`}
                >
                  {pendingCommentary.length === 0
                    ? "All sections verified"
                    : `${pendingCommentary.length} section${pendingCommentary.length === 1 ? "" : "s"} requiring review`}
                </span>
              </div>
            </div>
          </div>

          <div className="quartz-page-toolbar">
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/chat`}
            >
              <QuartzIcon className="quartz-inline-icon" name="assistant" />
              Open Assistant
            </Link>
            {selectedArtifactHref ? (
              <a className="secondary-button" href={selectedArtifactHref}>
                Preview Final
              </a>
            ) : (
              <button className="secondary-button" disabled type="button">
                Preview Final
              </button>
            )}
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

        <section className="quartz-section quartz-reporting-layout">
          <div className="quartz-reporting-left">
            <article className="quartz-card quartz-card-table-shell">
              <div className="quartz-section-header">
                <h2 className="quartz-section-title">Report Generation History</h2>
                <span className="quartz-queue-meta">
                  {reportRuns.length} run{reportRuns.length === 1 ? "" : "s"}
                </span>
              </div>

              <table className="quartz-table">
                <thead>
                  <tr>
                    <th>Version</th>
                    <th>Generated by</th>
                    <th>Timestamp</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {reportRuns.length === 0 ? (
                    <tr>
                      <td colSpan={4}>
                        <div className="quartz-empty-state">
                          No report runs exist yet. Generate the first pack to create released
                          artifacts and governed commentary.
                        </div>
                      </td>
                    </tr>
                  ) : (
                    reportRuns.map((run) => {
                      const isSelected = selectedReportRunId === run.id;
                      const tone = resolveReportRunStatusTone(run.status);
                      return (
                        <tr
                          className={isSelected ? "quartz-table-row selected" : undefined}
                          key={run.id}
                          onClick={() => {
                            void handleSelectRun(run.id);
                          }}
                        >
                          <td>
                            <div className="quartz-table-primary">v{run.version_no}</div>
                            <div className="quartz-table-secondary">
                              {run.failure_reason ?? "Governed report snapshot"}
                            </div>
                          </td>
                          <td>{formatGeneratedBy(run.generated_by_user_id)}</td>
                          <td>{formatTimestamp(run.completed_at ?? run.created_at)}</td>
                          <td>
                            <span className={`quartz-status-badge ${tone}`}>
                              {formatLabel(run.status)}
                            </span>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </article>

            <article className="quartz-card quartz-report-preview-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <h2 className="quartz-section-title">Financial Statements Preview</h2>
                  <p className="quartz-table-secondary">
                    {selectedArtifact ? formatLabel(selectedArtifact.artifactType) : "Select a run"}
                  </p>
                </div>
                {selectedArtifactHref ? (
                  <a className="quartz-filter-link" href={selectedArtifactHref}>
                    Open artifact
                  </a>
                ) : null}
              </div>

              {selectedArtifact === null ? (
                <div className="quartz-empty-state">
                  Select a report run to inspect its generated artifact set.
                </div>
              ) : (
                <>
                  {artifactRefs.length > 1 ? (
                    <div className="quartz-filter-chip-row">
                      {artifactRefs.map((artifactRef, index) => {
                        const key = getArtifactSelectionKey(artifactRef, index);
                        const label = formatLabel(
                          readArtifactString(artifactRef, "type", "artifact"),
                        );
                        return (
                          <button
                            className={
                              selectedArtifact.key === key
                                ? "quartz-filter-chip active"
                                : "quartz-filter-chip"
                            }
                            key={key}
                            onClick={() => setSelectedArtifactKey(key)}
                            type="button"
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                  ) : null}

                  <div className="quartz-report-preview-frame">
                    <div className="quartz-report-preview-sheet">
                      <p className="quartz-report-preview-entity">{entityName}</p>
                      <h3>{selectedArtifact.filename}</h3>
                      <p className="quartz-report-preview-period">
                        For {formatCloseRunPeriod(closeRun)}
                      </p>
                      <div className="quartz-report-preview-lines">
                        <div />
                        <div />
                        <div />
                        <div />
                        <div />
                      </div>
                    </div>
                  </div>

                  <div className="quartz-summary-list">
                    <div className="quartz-summary-row">
                      <span className="quartz-table-secondary">Storage key</span>
                      <strong>{selectedArtifact.storageKey}</strong>
                    </div>
                    <div className="quartz-summary-row">
                      <span className="quartz-table-secondary">Artifact size</span>
                      <strong>{formatBytes(selectedArtifact.sizeBytes)}</strong>
                    </div>
                    <div className="quartz-summary-row">
                      <span className="quartz-table-secondary">Run status</span>
                      <strong>{formatLabel(reportRunDetail?.status ?? "pending")}</strong>
                    </div>
                  </div>
                </>
              )}
            </article>
          </div>

          <div className="quartz-reporting-right">
            <article className={pendingCommentary.length > 0 ? "quartz-card ai" : "quartz-card"}>
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow secondary">Key variances identified</p>
                  <h3 className="quartz-kpi-heading">
                    {pendingCommentary.length > 0
                      ? "Narrative review is still open"
                      : "Narrative package is governed and ready"}
                  </h3>
                </div>
                <span className="quartz-compact-pill warning">
                  <QuartzIcon className="quartz-inline-icon" name="warning" />
                  Auto-generated
                </span>
              </div>

              <p className="form-helper">
                {pendingCommentary.length > 0
                  ? "Omni-AI has flagged sections that still need accountant narrative or controller verification before final sign-off."
                  : "All generated commentary sections are verified against the latest report run."}
              </p>

              <div className="quartz-variance-chip-row">
                {(pendingCommentary.length > 0 ? pendingCommentary : commentaryEntries)
                  .slice(0, 3)
                  .map((entry) => (
                    <span className="quartz-compact-pill warning" key={entry.id}>
                      {formatSectionLabel(entry.section_key)}
                    </span>
                  ))}
              </div>
            </article>

            <article className="quartz-card quartz-commentary-workbench">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <h2 className="quartz-section-title">Management Narrative</h2>
                  <p className="quartz-table-secondary">
                    {reportRunDetail
                      ? `Run v${reportRunDetail.version_no}`
                      : "No report run selected"}
                  </p>
                </div>
                {selectedCommentary ? (
                  <span
                    className={`quartz-status-badge ${resolveCommentaryStatusTone(selectedCommentary.status)}`}
                  >
                    {formatLabel(selectedCommentary.status)}
                  </span>
                ) : null}
              </div>

              {reportRunDetail === null ||
              commentaryEntries.length === 0 ||
              selectedCommentary === null ? (
                <div className="quartz-empty-state">
                  Commentary will appear here after a report run has been generated with narrative
                  sections.
                </div>
              ) : (
                <div className="quartz-commentary-layout">
                  <div className="quartz-commentary-queue">
                    {commentaryEntries.map((entry) => (
                      <button
                        className={
                          entry.section_key === selectedCommentary.section_key
                            ? "quartz-commentary-queue-item active"
                            : "quartz-commentary-queue-item"
                        }
                        key={entry.id}
                        onClick={() => setSelectedSectionKey(entry.section_key)}
                        type="button"
                      >
                        <div>
                          <strong>{formatSectionLabel(entry.section_key)}</strong>
                          <span>{truncateCommentary(entry.body)}</span>
                        </div>
                        <span
                          className={`quartz-status-badge ${resolveCommentaryStatusTone(entry.status)}`}
                        >
                          {formatLabel(entry.status)}
                        </span>
                      </button>
                    ))}
                  </div>

                  <div className="quartz-commentary-editor">
                    <div className="quartz-commentary-editor-meta">
                      <span>Last edited {formatTimestamp(selectedCommentary.updated_at)}</span>
                      <span>
                        {selectedCommentary.authored_by_user_id === null
                          ? "System draft"
                          : "Operator edited"}
                      </span>
                    </div>

                    <textarea
                      className="text-input quartz-commentary-textarea"
                      disabled={isSavingSectionKey === selectedCommentary.section_key}
                      onChange={(event) =>
                        setDraftCommentary((current) => ({
                          ...current,
                          [selectedCommentary.section_key]: event.target.value,
                        }))
                      }
                      value={
                        draftCommentary[selectedCommentary.section_key] ?? selectedCommentary.body
                      }
                    />

                    <div className="quartz-highlight-box">
                      <span className="quartz-table-secondary">Current narrative status</span>
                      <p className="form-helper">
                        {selectedCommentary.status === "approved"
                          ? "This section is already verified for the selected report run."
                          : "Save your edits, then mark the narrative as verified once the wording is ready for sign-off."}
                      </p>
                    </div>

                    <div className="quartz-button-row">
                      {pendingCommentary.length === 0 ? (
                        <Link
                          className="secondary-button"
                          href={`/entities/${entityId}/close-runs/${closeRunId}/exports`}
                        >
                          Proceed to Sign-Off
                        </Link>
                      ) : null}
                      <button
                        className="secondary-button"
                        disabled={isSavingSectionKey === selectedCommentary.section_key}
                        onClick={() => {
                          void handleSaveCommentary(selectedCommentary.section_key);
                        }}
                        type="button"
                      >
                        {isSavingSectionKey === selectedCommentary.section_key
                          ? "Saving..."
                          : "Save Draft"}
                      </button>
                      <button
                        className="primary-button"
                        disabled={
                          isSavingSectionKey === selectedCommentary.section_key ||
                          selectedCommentary.status === "approved"
                        }
                        onClick={() => {
                          void handleApproveCommentary(selectedCommentary.section_key);
                        }}
                        type="button"
                      >
                        {selectedCommentary.status === "approved"
                          ? "Verified"
                          : isSavingSectionKey === selectedCommentary.section_key
                            ? "Saving..."
                            : "Mark as Verified"}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}

function deriveSelectedArtifact(
  artifactRefs: readonly Record<string, unknown>[],
  selectedArtifactKey: string | null,
): SelectedArtifact | null {
  if (artifactRefs.length === 0) {
    return null;
  }

  const selectedIndex =
    artifactRefs.findIndex(
      (artifactRef, index) => getArtifactSelectionKey(artifactRef, index) === selectedArtifactKey,
    ) || 0;
  const safeIndex = selectedIndex >= 0 ? selectedIndex : 0;
  const artifactRef = artifactRefs[safeIndex]!;
  const artifactType = readArtifactString(artifactRef, "type", "artifact");
  return {
    artifactRef,
    artifactType,
    filename: readArtifactString(artifactRef, "filename", `Artifact ${safeIndex + 1}`),
    key: getArtifactSelectionKey(artifactRef, safeIndex),
    sizeBytes: readArtifactNumber(artifactRef, "size_bytes"),
    storageKey: readArtifactString(artifactRef, "storage_key", "Unavailable"),
  };
}

function deriveReportingReadiness(reportRunDetail: ReportRunDetailRecord | null): number {
  if (reportRunDetail === null) {
    return 0;
  }

  const runWeight =
    reportRunDetail.status === "completed"
      ? 55
      : reportRunDetail.status === "in_progress"
        ? 35
        : reportRunDetail.status === "failed"
          ? 10
          : 20;
  const commentaryWeight =
    reportRunDetail.commentary.length === 0
      ? 0
      : Math.round(
          (reportRunDetail.commentary.filter((entry) => entry.status === "approved").length /
            reportRunDetail.commentary.length) *
            40,
        );
  const artifactWeight = reportRunDetail.artifact_refs.length > 0 ? 5 : 0;
  return Math.min(100, runWeight + commentaryWeight + artifactWeight);
}

function resolveReportRunStatusTone(status: string): "error" | "neutral" | "success" | "warning" {
  switch (status) {
    case "completed":
      return "success";
    case "failed":
      return "error";
    case "in_progress":
      return "warning";
    default:
      return "neutral";
  }
}

function resolveCommentaryStatusTone(status: string): "error" | "neutral" | "success" | "warning" {
  switch (status) {
    case "approved":
      return "success";
    case "rejected":
      return "error";
    case "draft":
      return "warning";
    default:
      return "neutral";
  }
}

function formatGeneratedBy(userId: string | null): string {
  return userId === null ? "Omni-AI" : "Controller";
}

function formatSectionLabel(sectionKey: string): string {
  return sectionKey.replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
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

function getArtifactSelectionKey(artifactRef: Record<string, unknown>, index: number): string {
  return `${readArtifactString(artifactRef, "type", "artifact")}:${index}`;
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

function truncateCommentary(body: string): string {
  const normalized = body.trim();
  if (normalized.length === 0) {
    return "Pending draft...";
  }
  return normalized.length > 60 ? `${normalized.slice(0, 57)}...` : normalized;
}

function resolveReportErrorMessage(error: unknown): string {
  if (error instanceof ReportApiError) {
    return error.message;
  }
  if (error instanceof CloseRunApiError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return "The reporting workspace request failed.";
}
