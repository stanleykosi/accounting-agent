/*
Purpose: Surface close-run readiness, workflow phases, and chat-native intake
controls inside the accounting agent workbench.
Scope: COA awareness, source-document upload, blocker/warning rendering, and
operator next-step guidance without leaving the chat workspace.
Dependencies: React client hooks, same-origin COA/document upload helpers, and
Next.js links for deeper workspace drill-down.
*/

"use client";

import Link from "next/link";
import {
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type CSSProperties,
  type ReactNode,
  type ReactElement,
} from "react";
import { CoaApiError, uploadManualCoa } from "../../lib/coa";
import {
  DocumentReviewApiError,
  uploadSourceDocuments,
} from "../../lib/documents";
import type { ChatThreadWorkspace } from "../../lib/chat";

type AgentReadinessPanelProps = {
  closeRunId: string | undefined;
  entityId: string;
  onRefresh: () => Promise<void> | void;
  workspace: ChatThreadWorkspace | null;
};

export function AgentReadinessPanel({
  closeRunId,
  entityId,
  onRefresh,
  workspace,
}: Readonly<AgentReadinessPanelProps>): ReactElement {
  const [coaError, setCoaError] = useState<string | null>(null);
  const [coaFile, setCoaFile] = useState<File | null>(null);
  const [coaSuccess, setCoaSuccess] = useState<string | null>(null);
  const [documentError, setDocumentError] = useState<string | null>(null);
  const [documentFiles, setDocumentFiles] = useState<readonly File[]>([]);
  const [documentSuccess, setDocumentSuccess] = useState<string | null>(null);
  const [isUploadingCoa, setIsUploadingCoa] = useState(false);
  const [isUploadingDocuments, setIsUploadingDocuments] = useState(false);
  const coaInputRef = useRef<HTMLInputElement | null>(null);
  const documentInputRef = useRef<HTMLInputElement | null>(null);

  const accountPreview = useMemo(
    () =>
      (workspace?.coa.accounts ?? [])
        .filter((account) => account.is_postable)
        .slice(0, 8),
    [workspace],
  );

  if (workspace === null) {
    return (
      <p style={emptyStateStyle}>
        Select a thread to inspect workflow readiness, chart-of-accounts state, and upload controls.
      </p>
    );
  }

  const readiness = workspace.readiness;
  const coa = workspace.coa;
  const showCoaUpload = !coa.is_available || coa.requires_operator_upload;
  const showDocumentUpload = typeof closeRunId === "string" && closeRunId.length > 0;

  const handleCoaFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    setCoaFile(event.target.files?.[0] ?? null);
    setCoaError(null);
    setCoaSuccess(null);
  };

  const handleDocumentFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    setDocumentFiles(Array.from(event.target.files ?? []));
    setDocumentError(null);
    setDocumentSuccess(null);
  };

  const handleCoaUpload = async (): Promise<void> => {
    if (coaFile === null) {
      setCoaError("Select a CSV or Excel chart-of-accounts file to upload.");
      return;
    }

    setIsUploadingCoa(true);
    setCoaError(null);
    setCoaSuccess(null);
    try {
      await uploadManualCoa(entityId, coaFile);
      setCoaFile(null);
      if (coaInputRef.current !== null) {
        coaInputRef.current.value = "";
      }
      setCoaSuccess("Chart of accounts uploaded and activated. The agent workspace is refreshing.");
      await onRefresh();
    } catch (error: unknown) {
      if (error instanceof CoaApiError) {
        setCoaError(error.message);
      } else if (error instanceof Error && error.message.trim().length > 0) {
        setCoaError(error.message);
      } else {
        setCoaError("The chart of accounts could not be uploaded. Retry the file.");
      }
    } finally {
      setIsUploadingCoa(false);
    }
  };

  const handleDocumentUpload = async (): Promise<void> => {
    if (!showDocumentUpload || closeRunId === undefined) {
      setDocumentError("Open a close-run-scoped thread before uploading source documents.");
      return;
    }
    if (documentFiles.length === 0) {
      setDocumentError("Choose at least one PDF, CSV, or Excel source document to upload.");
      return;
    }

    setIsUploadingDocuments(true);
    setDocumentError(null);
    setDocumentSuccess(null);
    try {
      const result = await uploadSourceDocuments(entityId, closeRunId, documentFiles);
      setDocumentFiles([]);
      if (documentInputRef.current !== null) {
        documentInputRef.current.value = "";
      }
      setDocumentSuccess(
        result.uploadedCount === 1
          ? "1 source document uploaded and queued for parsing."
          : `${result.uploadedCount} source documents uploaded and queued for parsing.`,
      );
      await onRefresh();
    } catch (error: unknown) {
      if (error instanceof DocumentReviewApiError) {
        setDocumentError(error.message);
      } else if (error instanceof Error && error.message.trim().length > 0) {
        setDocumentError(error.message);
      } else {
        setDocumentError("The source-document batch could not be uploaded. Retry the files.");
      }
    } finally {
      setIsUploadingDocuments(false);
    }
  };

  return (
    <div style={panelStackStyle}>
      <div style={metricGridStyle}>
        <MetricTile label="Readiness" value={formatReadinessStatus(readiness.status)} />
        <MetricTile label="COA source" value={formatCoaSource(coa.source, coa.status)} />
        <MetricTile label="Documents" value={String(readiness.document_count)} />
        <MetricTile label="Parsed" value={String(readiness.parsed_document_count)} />
      </div>

      {coa.summary ? <Banner tone={showCoaUpload ? "warning" : "info"}>{coa.summary}</Banner> : null}

      {readiness.blockers.length > 0 ? (
        <NoticeCard title="Blocked until resolved" tone="danger">
          {readiness.blockers.map((item) => (
            <p key={item} style={noticeItemStyle}>
              {item}
            </p>
          ))}
        </NoticeCard>
      ) : null}

      {readiness.warnings.length > 0 ? (
        <NoticeCard title="Operator attention" tone="warning">
          {readiness.warnings.map((item) => (
            <p key={item} style={noticeItemStyle}>
              {item}
            </p>
          ))}
        </NoticeCard>
      ) : null}

      {readiness.next_actions.length > 0 ? (
        <section style={sectionStyle}>
          <div style={sectionHeaderStyle}>
            <div>
              <p style={sectionEyebrowStyle}>Next Actions</p>
              <h4 style={sectionTitleStyle}>What the agent expects next</h4>
            </div>
          </div>
          <div style={listStackStyle}>
            {readiness.next_actions.map((action) => (
              <div key={action} style={nextActionCardStyle}>
                {action}
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {showDocumentUpload ? (
        <section style={sectionStyle}>
          <div style={sectionHeaderStyle}>
            <div>
              <p style={sectionEyebrowStyle}>Step 06</p>
              <h4 style={sectionTitleStyle}>Supporting schedule workspace</h4>
            </div>
            <Link href={`/entities/${entityId}/close-runs/${closeRunId}/schedules`} style={sectionLinkStyle}>
              Open schedules
            </Link>
          </div>
          <p style={helperTextStyle}>
            Fixed assets, loan amortisation, accrual tracker, and budget-vs-actual workpapers are
            maintained in the Step 06 editor and now feed the reconciliation gate directly.
          </p>
        </section>
      ) : null}

      {readiness.phase_states.length > 0 ? (
        <section style={sectionStyle}>
          <div style={sectionHeaderStyle}>
            <div>
              <p style={sectionEyebrowStyle}>Workflow</p>
              <h4 style={sectionTitleStyle}>Close-run phase timeline</h4>
            </div>
          </div>
          <div style={listStackStyle}>
            {readiness.phase_states.map((phase) => (
              <article key={phase.phase} style={phaseCardStyle}>
                <div style={phaseHeaderStyle}>
                  <strong style={phaseLabelStyle}>{phase.label}</strong>
                  <span style={phaseStatusPillStyle(phase.status)}>{formatPhaseStatus(phase.status)}</span>
                </div>
                {phase.blocking_reason ? <p style={phaseReasonStyle}>{phase.blocking_reason}</p> : null}
                {phase.completed_at ? (
                  <p style={phaseMetaStyle}>Completed {formatTimestamp(phase.completed_at)}</p>
                ) : null}
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {showCoaUpload ? (
        <section style={sectionStyle}>
          <div style={sectionHeaderStyle}>
            <div>
              <p style={sectionEyebrowStyle}>Chart of Accounts</p>
              <h4 style={sectionTitleStyle}>Upload production COA</h4>
            </div>
            <Link href={`/entities/${entityId}/coa`} style={sectionLinkStyle}>
              Open COA workspace
            </Link>
          </div>
          <p style={helperTextStyle}>
            Uploading a production chart here updates the same canonical COA service the agent uses
            for recommendations, journals, reconciliation, and reports.
          </p>
          <label style={inputShellStyle}>
            <span style={inputLabelStyle}>Select COA file</span>
            <input
              accept=".csv,.xlsx,.xls,.xlsm"
              onChange={handleCoaFileChange}
              ref={coaInputRef}
              style={fileInputStyle}
              type="file"
            />
          </label>
          {coaFile ? <FileToken file={coaFile} /> : null}
          {coaSuccess ? <Banner tone="success">{coaSuccess}</Banner> : null}
          {coaError ? <Banner tone="danger">{coaError}</Banner> : null}
          <div style={actionRowStyle}>
            <button
              disabled={isUploadingCoa || coaFile === null}
              onClick={() => {
                void handleCoaUpload();
              }}
              style={primaryButtonStyle}
              type="button"
            >
              {isUploadingCoa ? "Uploading COA..." : "Upload chart of accounts"}
            </button>
          </div>
        </section>
      ) : null}

      {showDocumentUpload ? (
        <section style={sectionStyle}>
          <div style={sectionHeaderStyle}>
            <div>
              <p style={sectionEyebrowStyle}>Source Intake</p>
              <h4 style={sectionTitleStyle}>Upload source documents</h4>
            </div>
            <Link href={`/entities/${entityId}/close-runs/${closeRunId}/documents`} style={sectionLinkStyle}>
              Open document queue
            </Link>
          </div>
          <p style={helperTextStyle}>
            Files uploaded here go through the hosted document pipeline immediately. Parsing begins
            automatically, and the agent can then move into review, recommendation, and close
            actions from chat.
          </p>
          <label style={inputShellStyle}>
            <span style={inputLabelStyle}>Select files</span>
            <input
              accept=".pdf,.csv,.xlsx,.xls,.xlsm"
              multiple
              onChange={handleDocumentFileChange}
              ref={documentInputRef}
              style={fileInputStyle}
              type="file"
            />
          </label>
          {documentFiles.length > 0 ? (
            <div style={fileListStyle}>
              {documentFiles.map((file) => (
                <FileToken file={file} key={`${file.name}:${file.size}`} />
              ))}
            </div>
          ) : null}
          {documentSuccess ? <Banner tone="success">{documentSuccess}</Banner> : null}
          {documentError ? <Banner tone="danger">{documentError}</Banner> : null}
          <div style={actionRowStyle}>
            <button
              disabled={isUploadingDocuments || documentFiles.length === 0}
              onClick={() => {
                void handleDocumentUpload();
              }}
              style={primaryButtonStyle}
              type="button"
            >
              {isUploadingDocuments ? "Uploading documents..." : "Upload source documents"}
            </button>
          </div>
        </section>
      ) : null}

      {accountPreview.length > 0 ? (
        <section style={sectionStyle}>
          <div style={sectionHeaderStyle}>
            <div>
              <p style={sectionEyebrowStyle}>COA Context</p>
              <h4 style={sectionTitleStyle}>Accounts visible to the agent</h4>
            </div>
          </div>
          <div style={accountGridStyle}>
            {accountPreview.map((account) => (
              <article key={account.account_code} style={accountCardStyle}>
                <strong style={accountCodeStyle}>{account.account_code}</strong>
                <span style={accountNameStyle}>{account.account_name}</span>
                <span style={accountMetaStyle}>{account.account_type.replaceAll("_", " ")}</span>
              </article>
            ))}
          </div>
          <p style={helperTextStyle}>
            The planner sees the active account set and uses it when mapping recommendations,
            drafting journals, and explaining close-run state.
          </p>
        </section>
      ) : null}
    </div>
  );
}

function MetricTile({ label, value }: Readonly<{ label: string; value: string }>) {
  return (
    <div style={metricTileStyle}>
      <span style={metricLabelStyle}>{label}</span>
      <strong style={metricValueStyle}>{value}</strong>
    </div>
  );
}

function NoticeCard({
  children,
  title,
  tone,
}: Readonly<{
  children: ReactNode;
  title: string;
  tone: "danger" | "warning";
}>): ReactElement {
  return (
    <section style={tone === "danger" ? dangerNoticeStyle : warningNoticeStyle}>
      <div style={noticeTitleStyle}>{title}</div>
      <div style={listStackStyle}>{children}</div>
    </section>
  );
}

function Banner({
  children,
  tone,
}: Readonly<{
  children: ReactNode;
  tone: "danger" | "info" | "success" | "warning";
}>): ReactElement {
  return <div style={bannerStyle(tone)}>{children}</div>;
}

function FileToken({ file }: Readonly<{ file: File }>): ReactElement {
  return (
    <div style={fileTokenStyle}>
      <strong style={fileNameStyle}>{file.name}</strong>
      <span style={fileSizeStyle}>{formatByteSize(file.size)}</span>
    </div>
  );
}

function formatByteSize(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

function formatReadinessStatus(value: string): string {
  switch (value) {
    case "ready":
      return "Ready";
    case "attention_required":
      return "Attention";
    case "blocked":
      return "Blocked";
    default:
      return "Not scoped";
  }
}

function formatCoaSource(source: string | null, status: string): string {
  if (!source) {
    return status === "missing" ? "Missing" : "Unspecified";
  }
  return source.replaceAll("_", " ");
}

function formatPhaseStatus(value: string): string {
  return value.replaceAll("_", " ");
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return value;
  }
  return date.toLocaleString();
}

function phaseStatusPillStyle(status: string): CSSProperties {
  const palette =
    status === "completed"
      ? { background: "rgba(62, 163, 118, 0.18)", color: "#8FDBC5" }
      : status === "blocked"
        ? { background: "rgba(217, 83, 79, 0.16)", color: "#FFB4AE" }
        : status === "in_progress"
          ? { background: "rgba(76, 139, 245, 0.18)", color: "#9BC2FF" }
          : { background: "rgba(183, 195, 214, 0.12)", color: "#D7E0ED" };
  return {
    ...palette,
    borderRadius: 999,
    fontSize: 11,
    fontWeight: 700,
    padding: "4px 10px",
    textTransform: "capitalize",
  };
}

function bannerStyle(tone: "danger" | "info" | "success" | "warning"): CSSProperties {
  switch (tone) {
    case "danger":
      return {
        background: "rgba(217, 83, 79, 0.16)",
        border: "1px solid rgba(217, 83, 79, 0.3)",
        borderRadius: 12,
        color: "#FFB4AE",
        fontSize: 13,
        lineHeight: "20px",
        padding: "10px 12px",
      };
    case "success":
      return {
        background: "rgba(62, 163, 118, 0.16)",
        border: "1px solid rgba(62, 163, 118, 0.28)",
        borderRadius: 12,
        color: "#8FDBC5",
        fontSize: 13,
        lineHeight: "20px",
        padding: "10px 12px",
      };
    case "warning":
      return {
        background: "rgba(231, 169, 59, 0.16)",
        border: "1px solid rgba(231, 169, 59, 0.28)",
        borderRadius: 12,
        color: "#FFD38D",
        fontSize: 13,
        lineHeight: "20px",
        padding: "10px 12px",
      };
    default:
      return {
        background: "rgba(76, 139, 245, 0.16)",
        border: "1px solid rgba(76, 139, 245, 0.28)",
        borderRadius: 12,
        color: "#A8CBFF",
        fontSize: 13,
        lineHeight: "20px",
        padding: "10px 12px",
      };
  }
}

const panelStackStyle: CSSProperties = {
  display: "grid",
  gap: 12,
};

const emptyStateStyle: CSSProperties = {
  color: "#AAB7CA",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
};

const metricGridStyle: CSSProperties = {
  display: "grid",
  gap: 10,
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
};

const metricTileStyle: CSSProperties = {
  background: "#152033",
  border: "1px solid #24324A",
  borderRadius: 14,
  display: "grid",
  gap: 6,
  padding: "12px 14px",
};

const metricLabelStyle: CSSProperties = {
  color: "#8FA2BF",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.06em",
  textTransform: "uppercase",
};

const metricValueStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 14,
  fontWeight: 700,
  textTransform: "capitalize",
};

const dangerNoticeStyle: CSSProperties = {
  background: "#25171B",
  border: "1px solid rgba(217, 83, 79, 0.28)",
  borderRadius: 14,
  display: "grid",
  gap: 10,
  padding: "14px",
};

const warningNoticeStyle: CSSProperties = {
  background: "#241D11",
  border: "1px solid rgba(231, 169, 59, 0.28)",
  borderRadius: 14,
  display: "grid",
  gap: 10,
  padding: "14px",
};

const noticeTitleStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 13,
  fontWeight: 700,
};

const noticeItemStyle: CSSProperties = {
  color: "#D7E0ED",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
};

const sectionStyle: CSSProperties = {
  background: "#101828",
  border: "1px solid #24324A",
  borderRadius: 16,
  display: "grid",
  gap: 12,
  padding: "16px",
};

const sectionHeaderStyle: CSSProperties = {
  alignItems: "start",
  display: "flex",
  gap: 12,
  justifyContent: "space-between",
};

const sectionEyebrowStyle: CSSProperties = {
  color: "#7EBCFF",
  fontSize: 11,
  fontWeight: 700,
  letterSpacing: "0.08em",
  margin: 0,
  textTransform: "uppercase",
};

const sectionTitleStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 15,
  fontWeight: 700,
  margin: "4px 0 0",
};

const sectionLinkStyle: CSSProperties = {
  color: "#9BC2FF",
  fontSize: 12,
  fontWeight: 600,
  textDecoration: "none",
  whiteSpace: "nowrap",
};

const helperTextStyle: CSSProperties = {
  color: "#AAB7CA",
  fontSize: 13,
  lineHeight: "20px",
  margin: 0,
};

const inputShellStyle: CSSProperties = {
  background: "#152033",
  border: "1px dashed #35507A",
  borderRadius: 14,
  display: "grid",
  gap: 8,
  padding: "12px",
};

const inputLabelStyle: CSSProperties = {
  color: "#D7E0ED",
  fontSize: 12,
  fontWeight: 600,
};

const fileInputStyle: CSSProperties = {
  color: "#B7C3D6",
  fontSize: 12,
};

const fileListStyle: CSSProperties = {
  display: "grid",
  gap: 8,
};

const fileTokenStyle: CSSProperties = {
  alignItems: "center",
  background: "#152033",
  border: "1px solid #24324A",
  borderRadius: 12,
  display: "flex",
  gap: 8,
  justifyContent: "space-between",
  padding: "10px 12px",
};

const fileNameStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 12,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const fileSizeStyle: CSSProperties = {
  color: "#8FA2BF",
  fontSize: 11,
  flexShrink: 0,
};

const actionRowStyle: CSSProperties = {
  display: "flex",
  justifyContent: "flex-start",
};

const primaryButtonStyle: CSSProperties = {
  background: "linear-gradient(135deg, #4C8BF5 0%, #3569D3 100%)",
  border: "none",
  borderRadius: 12,
  color: "#FFFFFF",
  cursor: "pointer",
  fontSize: 13,
  fontWeight: 700,
  padding: "10px 14px",
};

const listStackStyle: CSSProperties = {
  display: "grid",
  gap: 8,
};

const nextActionCardStyle: CSSProperties = {
  background: "#152033",
  border: "1px solid #24324A",
  borderRadius: 12,
  color: "#D7E0ED",
  fontSize: 13,
  lineHeight: "20px",
  padding: "10px 12px",
};

const phaseCardStyle: CSSProperties = {
  background: "#152033",
  border: "1px solid #24324A",
  borderRadius: 12,
  display: "grid",
  gap: 8,
  padding: "12px",
};

const phaseHeaderStyle: CSSProperties = {
  alignItems: "center",
  display: "flex",
  gap: 10,
  justifyContent: "space-between",
};

const phaseLabelStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 13,
};

const phaseReasonStyle: CSSProperties = {
  color: "#D7E0ED",
  fontSize: 12,
  lineHeight: "18px",
  margin: 0,
};

const phaseMetaStyle: CSSProperties = {
  color: "#8FA2BF",
  fontSize: 11,
  margin: 0,
};

const accountGridStyle: CSSProperties = {
  display: "grid",
  gap: 8,
  gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
};

const accountCardStyle: CSSProperties = {
  background: "#152033",
  border: "1px solid #24324A",
  borderRadius: 12,
  display: "grid",
  gap: 4,
  padding: "10px 12px",
};

const accountCodeStyle: CSSProperties = {
  color: "#F4F7FB",
  fontSize: 12,
};

const accountNameStyle: CSSProperties = {
  color: "#D7E0ED",
  fontSize: 12,
  lineHeight: "18px",
};

const accountMetaStyle: CSSProperties = {
  color: "#8FA2BF",
  fontSize: 11,
  textTransform: "capitalize",
};
