/*
Purpose: Render the close-run document review queue and evidence-first exception workspace.
Scope: Queue loading, filter and selection state, side-by-side extraction context, and evidence drawer coordination.
Dependencies: Document review API helpers, queue/detail components, and shared UI surface/evidence primitives.
*/

"use client";

import { EvidenceDrawer, ReviewLayout, SurfaceCard } from "@accounting-ai-agent/ui";
import { use, useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import { DocumentUploadPanel } from "../../../../../../../components/documents/DocumentUploadPanel";
import { DocumentReviewTable } from "../../../../../../../components/documents/DocumentReviewTable";
import { ExtractionPanel } from "../../../../../../../components/documents/ExtractionPanel";
import {
  deleteSourceDocument,
  type DocumentVerificationChecklist,
  DocumentReviewApiError,
  filterDocumentReviewItems,
  formatPeriodLabel,
  persistDocumentReviewDecision,
  persistExtractedFieldCorrection,
  readDocumentReviewWorkspace,
  reparseSourceDocument,
  type DocumentReviewFilter,
  type DocumentReviewWorkspaceData,
  type EvidenceReference,
} from "../../../../../../../lib/documents";

type CloseRunDocumentsPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

type EvidenceDrawerState = {
  isOpen: boolean;
  references: readonly EvidenceReference[];
  sourceLabel: string;
  title: string;
};

const defaultVerificationChecklist: DocumentVerificationChecklist = {
  authorized: false,
  complete: false,
  period: false,
  transactionMatch: false,
};

const defaultEvidenceDrawerState: EvidenceDrawerState = {
  isOpen: false,
  references: [],
  sourceLabel: "Evidence",
  title: "Evidence references",
};

/**
 * Purpose: Compose the document exception queue workspace for one entity close run.
 * Inputs: Route params containing entity and close-run UUIDs.
 * Outputs: A client-rendered review workspace with queue table, extraction panel, and evidence drawer.
 * Behavior: Loads queue state from same-origin API routes and persists reviewer decisions and corrections through the backend workflow.
 */
export default function CloseRunDocumentsPage({
  params,
}: Readonly<CloseRunDocumentsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);

  const [activeFilter, setActiveFilter] = useState<DocumentReviewFilter>("all");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [deleteMutationDocumentId, setDeleteMutationDocumentId] = useState<string | null>(null);
  const [fieldMutationId, setFieldMutationId] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [operationMessage, setOperationMessage] = useState<string | null>(null);
  const [reparseMutationDocumentId, setReparseMutationDocumentId] = useState<string | null>(null);
  const [reviewMutationDocumentId, setReviewMutationDocumentId] = useState<string | null>(null);
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [verificationDrafts, setVerificationDrafts] = useState<
    Record<string, DocumentVerificationChecklist>
  >({});
  const [workspaceData, setWorkspaceData] = useState<DocumentReviewWorkspaceData | null>(null);
  const [evidenceDrawer, setEvidenceDrawer] = useState<EvidenceDrawerState>(defaultEvidenceDrawerState);

  const refreshWorkspace = useCallback(async (): Promise<void> => {
    await loadWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: (nextWorkspace) => {
        setWorkspaceData(nextWorkspace);
        setSelectedDocumentId((currentSelectedDocumentId) => {
          if (
            currentSelectedDocumentId !== null &&
            nextWorkspace.items.some((item) => item.id === currentSelectedDocumentId)
          ) {
            return currentSelectedDocumentId;
          }

          return selectInitialDocumentId(nextWorkspace);
        });
      },
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

  useEffect(() => {
    void refreshWorkspace();
  }, [refreshWorkspace]);

  const visibleItems = useMemo(
    () =>
      workspaceData === null
        ? []
        : filterDocumentReviewItems(workspaceData.items, activeFilter),
    [activeFilter, workspaceData],
  );

  const selectedDocument = useMemo(() => {
    if (workspaceData === null || selectedDocumentId === null) {
      return null;
    }

    return workspaceData.items.find((item) => item.id === selectedDocumentId) ?? null;
  }, [selectedDocumentId, workspaceData]);

  const selectedChecklist = useMemo(() => {
    if (selectedDocument === null) {
      return null;
    }
    return (
      verificationDrafts[selectedDocument.id] ?? deriveVerificationChecklistDraft(selectedDocument)
    );
  }, [selectedDocument, verificationDrafts]);

  const closeRunPeriodLabel =
    workspaceData === null
      ? null
      : formatPeriodLabel(workspaceData.closeRunPeriodStart, workspaceData.closeRunPeriodEnd);

  const handleFilterChange = (filter: DocumentReviewFilter): void => {
    setActiveFilter(filter);
    if (workspaceData === null) {
      return;
    }

    const nextVisibleItems = filterDocumentReviewItems(workspaceData.items, filter);
    if (nextVisibleItems.length === 0) {
      return;
    }

    if (!nextVisibleItems.some((item) => item.id === selectedDocumentId)) {
      setSelectedDocumentId(nextVisibleItems[0]?.id ?? null);
    }
  };

  const handleReviewAction = useCallback(
    async (
      documentId: string,
      decision: "approved" | "rejected" | "needs_info",
    ): Promise<void> => {
      setReviewMutationDocumentId(documentId);
      setOperationMessage(null);
      try {
        const checklist = verificationDrafts[documentId];
        await persistDocumentReviewDecision(
          entityId,
          closeRunId,
          documentId,
          decision,
          noteDraft.trim().length > 0 ? noteDraft : undefined,
          decision === "approved"
            ? (checklist ??
              deriveVerificationChecklistDraft(
                workspaceData?.items.find((item) => item.id === documentId) ?? null,
              ))
            : checklist,
        );
        setOperationMessage(
          decision === "approved"
            ? "Document approved and extraction state refreshed."
            : decision === "rejected"
              ? "Document rejection saved."
              : "Request-for-info decision saved.",
        );
        setNoteDraft("");
        setVerificationDrafts((current) => {
          const nextDrafts = { ...current };
          delete nextDrafts[documentId];
          return nextDrafts;
        });
        await refreshWorkspace();
      } catch (error) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setReviewMutationDocumentId(null);
      }
    },
    [closeRunId, entityId, noteDraft, refreshWorkspace, verificationDrafts, workspaceData],
  );

  const handleFieldCorrection = useCallback(
    async (input: {
      correctedType: string;
      correctedValue: string;
      fieldId: string;
    }): Promise<void> => {
      setFieldMutationId(input.fieldId);
      setOperationMessage(null);
      try {
        await persistExtractedFieldCorrection(
          entityId,
          closeRunId,
          input.fieldId,
          noteDraft.trim().length > 0
            ? {
                correctedType: input.correctedType,
                correctedValue: input.correctedValue,
                reason: noteDraft,
              }
            : {
                correctedType: input.correctedType,
                correctedValue: input.correctedValue,
              },
        );
        setOperationMessage("Field correction saved and the document returned to review.");
        setNoteDraft("");
        await refreshWorkspace();
      } catch (error) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setFieldMutationId(null);
      }
    },
    [closeRunId, entityId, noteDraft, refreshWorkspace],
  );

  const handleDeleteDocument = useCallback(
    async (documentId: string): Promise<void> => {
      const document =
        workspaceData?.items.find((candidate) => candidate.id === documentId) ?? null;
      if (document === null) {
        setErrorMessage("Select a document from the queue before deleting it.");
        return;
      }

      const confirmed = window.confirm(
        `Delete ${document.originalFilename} from this close run? This removes the uploaded file and linked extraction data.`,
      );
      if (!confirmed) {
        return;
      }

      setDeleteMutationDocumentId(documentId);
      setOperationMessage(null);
      try {
        const result = await deleteSourceDocument(entityId, closeRunId, documentId);
        setOperationMessage(
          result.deletedDocumentCount === 1
            ? `${result.deletedDocumentFilename} was deleted from the close run.`
            : `${result.deletedDocumentFilename} and ${result.deletedDocumentCount - 1} linked document(s) were deleted.`,
        );
        setNoteDraft("");
        setVerificationDrafts((current) => {
          const nextDrafts = { ...current };
          delete nextDrafts[documentId];
          return nextDrafts;
        });
        await refreshWorkspace();
      } catch (error) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setDeleteMutationDocumentId(null);
      }
    },
    [closeRunId, entityId, refreshWorkspace, workspaceData],
  );

  const handleReparseDocument = useCallback(
    async (documentId: string): Promise<void> => {
      const document =
        workspaceData?.items.find((candidate) => candidate.id === documentId) ?? null;
      if (document === null) {
        setErrorMessage("Select a document from the queue before reparsing it.");
        return;
      }

      const confirmed = window.confirm(
        `Reparse ${document.originalFilename}? This clears the current extraction, issues, and parse artifacts before queuing a fresh parse.`,
      );
      if (!confirmed) {
        return;
      }

      setReparseMutationDocumentId(documentId);
      setOperationMessage(null);
      try {
        const result = await reparseSourceDocument(entityId, closeRunId, documentId);
        setOperationMessage(
          `${result.reparsedDocumentFilename} was queued for reparsing. Cleared ${result.clearedExtractionCount} extraction(s), ${result.clearedIssueCount} issue(s), and ${result.clearedVersionCount} prior parse version(s).`,
        );
        setNoteDraft("");
        setVerificationDrafts((current) => {
          const nextDrafts = { ...current };
          delete nextDrafts[documentId];
          return nextDrafts;
        });
        await refreshWorkspace();
      } catch (error) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setReparseMutationDocumentId(null);
      }
    },
    [closeRunId, entityId, refreshWorkspace, workspaceData],
  );

  const handleOpenEvidenceForDocument = (documentId: string): void => {
    if (workspaceData === null) {
      return;
    }

    const document = workspaceData.items.find((item) => item.id === documentId);
    if (!document) {
      return;
    }

    setSelectedDocumentId(document.id);
    setEvidenceDrawer({
      isOpen: true,
      references: document.evidenceRefs,
      sourceLabel: document.originalFilename,
      title: "Document evidence",
    });
  };

  const handleOpenEvidence = (input: {
    references: readonly EvidenceReference[];
    sourceLabel: string;
    title: string;
  }): void => {
    setEvidenceDrawer({
      isOpen: true,
      references: input.references,
      sourceLabel: input.sourceLabel,
      title: input.title,
    });
  };

  const handleChecklistChange = useCallback(
    (field: keyof DocumentVerificationChecklist, nextValue: boolean): void => {
      if (selectedDocument === null) {
        return;
      }
      setVerificationDrafts((current) => ({
        ...current,
        [selectedDocument.id]: {
          ...(current[selectedDocument.id] ?? deriveVerificationChecklistDraft(selectedDocument)),
          [field]: nextValue,
        },
      }));
    },
    [selectedDocument],
  );

  if (isLoading) {
    return (
      <div className="app-shell document-review-page">
        <SurfaceCard title="Loading Document Queue" subtitle="Close run documents">
          <p className="form-helper">Loading document review queue, exceptions, and evidence context...</p>
        </SurfaceCard>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="app-shell document-review-page">
        <SurfaceCard title="Document Queue Unavailable" subtitle="Close run documents">
          <div className="status-banner danger" role="alert">
            {errorMessage ??
              "The document review workspace could not be loaded. Verify the entity and close-run IDs, then retry."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  return (
    <div className="app-shell document-review-page">
      <section className="hero-grid document-review-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Document Review Queue</p>
          <h1>Collection exceptions for evidence-first review.</h1>
          <p className="lede">
            Resolve low-confidence, blocked, duplicate, and wrong-period documents before the close
            run advances from Collection into Processing.
          </p>
        </div>

        <SurfaceCard title="Close-run Context" subtitle="Collection phase" tone="accent">
          <dl className="entity-meta-grid document-review-summary-grid">
            <div>
              <dt>Close run</dt>
              <dd>{workspaceData.closeRunId}</dd>
            </div>
            <div>
              <dt>Period</dt>
              <dd>{closeRunPeriodLabel}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{workspaceData.closeRunStatus.replaceAll("_", " ")}</dd>
            </div>
            <div>
              <dt>Confidence threshold</dt>
              <dd>{Math.round(workspaceData.confidenceThreshold * 100)}%</dd>
            </div>
          </dl>

          <div className="document-metric-row">
            <MetricChip label="Low confidence" value={workspaceData.queueCounts.low_confidence} />
            <MetricChip label="Blocked" value={workspaceData.queueCounts.blocked} />
            <MetricChip label="Duplicate" value={workspaceData.queueCounts.duplicate} />
            <MetricChip label="Wrong period" value={workspaceData.queueCounts.wrong_period} />
          </div>
        </SurfaceCard>
      </section>

      {errorMessage ? (
        <div className="status-banner warning" role="status">
          {errorMessage}
        </div>
      ) : null}

      {operationMessage ? (
        <div className="status-banner success" role="status">
          {operationMessage}
        </div>
      ) : null}

      <SurfaceCard title="Add Source Documents" subtitle="API-managed upload">
        <DocumentUploadPanel
          closeRunId={closeRunId}
          entityId={entityId}
          onUploadComplete={async () => {
            await refreshWorkspace();
          }}
        />
      </SurfaceCard>

      <ReviewLayout
        className="document-review-grid"
        main={
          <SurfaceCard title="Exception Queue" subtitle="Review table">
            <DocumentReviewTable
              activeFilter={activeFilter}
              items={visibleItems}
              onFilterChange={handleFilterChange}
              onOpenEvidence={handleOpenEvidenceForDocument}
              onSelectDocument={setSelectedDocumentId}
              queueCounts={workspaceData.queueCounts}
              reviewMutationDocumentId={reviewMutationDocumentId}
              selectedDocumentId={selectedDocumentId}
            />
          </SurfaceCard>
        }
        side={
          <div className="document-review-side-column">
            <SurfaceCard title="Extraction Context" subtitle="Selected document">
              <ExtractionPanel
                key={selectedDocument?.id ?? "no-document-selected"}
                actionNote={noteDraft}
                checklist={selectedChecklist}
                deleteMutationDocumentId={deleteMutationDocumentId}
                fieldMutationId={fieldMutationId}
                onChecklistChange={handleChecklistChange}
                onDeleteDocument={handleDeleteDocument}
                onReparseDocument={handleReparseDocument}
                onOpenEvidence={handleOpenEvidence}
                onFieldCorrection={handleFieldCorrection}
                onNoteChange={setNoteDraft}
                onReviewAction={handleReviewAction}
                reparseMutationDocumentId={reparseMutationDocumentId}
                reviewMutationDocumentId={reviewMutationDocumentId}
                selectedDocument={selectedDocument}
              />
            </SurfaceCard>

            <SurfaceCard title="Evidence Drawer" subtitle="Source-backed references">
              <EvidenceDrawer
                emptyMessage="Select a field or queue row to open source-backed evidence references."
                isOpen={evidenceDrawer.isOpen}
                onClose={() => setEvidenceDrawer(defaultEvidenceDrawerState)}
                references={evidenceDrawer.references}
                sourceLabel={evidenceDrawer.sourceLabel}
                title={evidenceDrawer.title}
              />
              {!evidenceDrawer.isOpen ? (
                <p className="form-helper">
                  Open evidence from the queue or extraction panel to inspect source metadata and
                  confidence traces.
                </p>
              ) : null}
            </SurfaceCard>
          </div>
        }
      />
    </div>
  );
}

/**
 * Purpose: Fetch and hydrate the document review workspace state for the current route.
 * Inputs: Route identifiers and page-level state update callbacks.
 * Outputs: None; callers receive deterministic state updates through provided callbacks.
 * Behavior: Captures API failures as operator-safe messages while preserving fail-fast diagnostics.
 */
async function loadWorkspace(options: {
  closeRunId: string;
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (workspace: DocumentReviewWorkspaceData) => void;
  onLoadingChange: (isLoading: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  options.onError(null);

  try {
    const workspace = await readDocumentReviewWorkspace(options.entityId, options.closeRunId);
    options.onLoaded(workspace);
  } catch (error: unknown) {
    if (error instanceof DocumentReviewApiError) {
      options.onError(error.message);
    } else {
      options.onError("Failed to load the document review queue. Reload and try again.");
    }
  } finally {
    options.onLoadingChange(false);
  }
}

/**
 * Purpose: Pick a stable initial selection for the queue details pane.
 * Inputs: Fully loaded workspace data.
 * Outputs: The document ID that should be focused first, or null when the queue is empty.
 * Behavior: Prioritizes exception rows so reviewers immediately land on actionable items.
 */
function selectInitialDocumentId(workspace: DocumentReviewWorkspaceData): string | null {
  return workspace.items.find((item) => item.hasException)?.id ?? workspace.items[0]?.id ?? null;
}

function resolveDocumentReviewErrorMessage(error: unknown): string {
  if (error instanceof DocumentReviewApiError) {
    if (error.code === "workflow_phase_locked") {
      return "Document review actions are only available during Collection. Rewind the close run to Collection or delete the mutable run to start over.";
    }
    return error.message;
  }

  return "The requested document review action could not be completed. Retry after refreshing the workspace.";
}

/**
 * Purpose: Render a compact numeric metric chip for queue summary cards.
 * Inputs: Metric label and integer count value.
 * Outputs: A short inline metric element used in the close-run context card.
 * Behavior: Keeps summary values compact without introducing additional dependency components.
 */
function MetricChip({
  label,
  value,
}: Readonly<{
  label: string;
  value: number;
}>): ReactElement {
  return (
    <div className="document-metric-chip">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function deriveVerificationChecklistDraft(
  document: DocumentReviewWorkspaceData["items"][number] | null,
): DocumentVerificationChecklist {
  if (document === null) {
    return defaultVerificationChecklist;
  }
  const autoTransactionMatchStatus = document.latestExtraction?.autoTransactionMatch?.status;
  if (document.status === "approved") {
    return {
      authorized: true,
      complete: true,
      period: true,
      transactionMatch: true,
    };
  }
  return {
    ...defaultVerificationChecklist,
    transactionMatch:
      autoTransactionMatchStatus === "matched" ||
      autoTransactionMatchStatus === "not_applicable",
  };
}
