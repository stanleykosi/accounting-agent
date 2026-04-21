/*
Purpose: Render the Quartz inputs workspace for one close run without losing the existing review actions.
Scope: Queue loading, search/filter state, selected-document review, evidence access, and source-document intake.
Dependencies: Document review API helpers, upload/review components, and shared Quartz workspace styles.
*/

"use client";

import { EvidenceDrawer } from "@accounting-ai-agent/ui";
import {
  use,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ChangeEvent,
  type ReactElement,
} from "react";
import { DocumentUploadPanel } from "../../../../../../../components/documents/DocumentUploadPanel";
import { ExtractionPanel } from "../../../../../../../components/documents/ExtractionPanel";
import { QuartzIcon } from "../../../../../../../components/layout/QuartzIcons";
import {
  deleteSourceDocument,
  type DocumentReviewFilter,
  type DocumentReviewQueueItem,
  type DocumentVerificationChecklist,
  type DocumentReviewWorkspaceData,
  DocumentReviewApiError,
  filterDocumentReviewItems,
  formatPeriodLabel,
  persistDocumentReviewDecision,
  persistExtractedFieldCorrection,
  readDocumentReviewWorkspace,
  reparseSourceDocument,
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

type QueueFilterDefinition = {
  filter: DocumentReviewFilter;
  label: string;
};

const filterDefinitions: readonly QueueFilterDefinition[] = [
  { filter: "all", label: "All Documents" },
  { filter: "low_confidence", label: "Low Confidence" },
  { filter: "blocked", label: "Blocked" },
  { filter: "duplicate", label: "Duplicates" },
  { filter: "wrong_period", label: "Wrong Period" },
];

const defaultVerificationChecklist: DocumentVerificationChecklist = {
  authorized: false,
  complete: false,
  period: false,
};

const defaultEvidenceDrawerState: EvidenceDrawerState = {
  isOpen: false,
  references: [],
  sourceLabel: "Evidence",
  title: "Evidence references",
};

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
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [verificationDrafts, setVerificationDrafts] = useState<
    Record<string, DocumentVerificationChecklist>
  >({});
  const [workspaceData, setWorkspaceData] = useState<DocumentReviewWorkspaceData | null>(null);
  const [evidenceDrawer, setEvidenceDrawer] = useState<EvidenceDrawerState>(
    defaultEvidenceDrawerState,
  );

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

  const filteredItems = useMemo(
    () =>
      workspaceData === null ? [] : filterDocumentReviewItems(workspaceData.items, activeFilter),
    [activeFilter, workspaceData],
  );

  const visibleItems = useMemo(() => {
    const normalizedSearch = searchQuery.trim().toLowerCase();
    if (normalizedSearch.length === 0) {
      return filteredItems;
    }

    return filteredItems.filter((item) =>
      [
        item.originalFilename,
        item.documentType,
        item.primaryIssueReason ?? "",
        item.issueTypes.join(" "),
        resolveDocumentStatusLabel(item),
      ]
        .join(" ")
        .toLowerCase()
        .includes(normalizedSearch),
    );
  }, [filteredItems, searchQuery]);

  const selectedDocument = useMemo(() => {
    if (workspaceData === null || selectedDocumentId === null) {
      return null;
    }

    return workspaceData.items.find((item) => item.id === selectedDocumentId) ?? null;
  }, [selectedDocumentId, workspaceData]);

  const pendingParseCount = useMemo(
    () => workspaceData?.items.filter((item) => item.status === "uploaded").length ?? 0,
    [workspaceData],
  );

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

  const readyItemsCount = useMemo(
    () =>
      workspaceData?.items.filter(
        (item) =>
          !item.hasException &&
          item.status !== "uploaded" &&
          item.status !== "processing" &&
          item.status !== "failed",
      ).length ?? 0,
    [workspaceData],
  );

  const nextException = useMemo(
    () => workspaceData?.items.find((item) => item.hasException) ?? null,
    [workspaceData],
  );

  const handleReviewAction = useCallback(
    async (documentId: string, decision: "approved" | "rejected" | "needs_info"): Promise<void> => {
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
      } catch (error: unknown) {
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
      } catch (error: unknown) {
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
            : `${result.deletedDocumentFilename} and ${
                result.deletedDocumentCount - 1
              } linked document(s) were deleted.`,
        );
        setNoteDraft("");
        setVerificationDrafts((current) => {
          const nextDrafts = { ...current };
          delete nextDrafts[documentId];
          return nextDrafts;
        });
        await refreshWorkspace();
      } catch (error: unknown) {
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
      } catch (error: unknown) {
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

  const handleSelectNextException = (): void => {
    if (nextException === null) {
      return;
    }

    setSelectedDocumentId(nextException.id);
    setActiveFilter("all");
  };

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading inputs workspace...</div>
        </section>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {errorMessage ??
              "The inputs workspace could not be loaded. Verify the entity and close-run IDs, then retry."}
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
            <p className="quartz-kpi-label">Current Close • {closeRunPeriodLabel}</p>
            <h1>Inputs Workspace</h1>
            <p className="quartz-page-subtitle">
              Evidence-first intake and issue clearing before the close advances into
              recommendations and journals.
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <label className="quartz-toolbar-search">
              <QuartzIcon className="quartz-inline-icon" name="filter" />
              <input
                className="text-input"
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  setSearchQuery(event.target.value)
                }
                placeholder="Filter or search documents"
                type="search"
                value={searchQuery}
              />
            </label>
            <button
              className="secondary-button"
              disabled={nextException === null}
              onClick={handleSelectNextException}
              type="button"
            >
              Review Next Exception
            </button>
            <a className="primary-button" href="#source-intake">
              Upload Evidence
            </a>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {errorMessage}
          </div>
        ) : null}

        {operationMessage ? (
          <div className="status-banner success quartz-section" role="status">
            {operationMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-filter-chip-row">
            {filterDefinitions.map((definition) => {
              const isActive = activeFilter === definition.filter;
              return (
                <button
                  className={isActive ? "quartz-filter-chip active" : "quartz-filter-chip"}
                  key={definition.filter}
                  onClick={() => setActiveFilter(definition.filter)}
                  type="button"
                >
                  <span>{definition.label}</span>
                  <strong>{workspaceData.queueCounts[definition.filter]}</strong>
                </button>
              );
            })}
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Document Name</th>
                  <th>Type</th>
                  <th>Intake Date</th>
                  <th>Amount (NGN)</th>
                  <th>Status</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {visibleItems.length === 0 ? (
                  <tr>
                    <td colSpan={6}>
                      <div className="quartz-empty-state">
                        No documents match the current search and filter combination.
                      </div>
                    </td>
                  </tr>
                ) : (
                  visibleItems.map((item) => {
                    const amount = extractDocumentAmount(item);
                    const statusTone = resolveDocumentStatusTone(item);
                    const isSelected = selectedDocumentId === item.id;

                    return (
                      <tr
                        className={[
                          item.hasException ? "quartz-table-row error" : "",
                          isSelected ? "quartz-table-row selected" : "",
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        key={item.id}
                        onClick={() => setSelectedDocumentId(item.id)}
                      >
                        <td>
                          <div className="quartz-table-primary">{item.originalFilename}</div>
                          <div className="quartz-table-secondary">
                            {item.primaryIssueReason ?? "Ready for accountant review"}
                          </div>
                        </td>
                        <td>{formatLabel(item.documentType)}</td>
                        <td>{formatDocumentDate(item.createdAt)}</td>
                        <td className="quartz-table-numeric">{amount ?? "-"}</td>
                        <td>
                          <span className={`quartz-status-badge ${statusTone}`}>
                            {resolveDocumentStatusLabel(item)}
                          </span>
                        </td>
                        <td>{formatSourceChannel(item.sourceChannel)}</td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-section-title">Selected Document Review</h2>
                {selectedDocument ? (
                  <button
                    className="secondary-button"
                    onClick={() => handleOpenEvidenceForDocument(selectedDocument.id)}
                    type="button"
                  >
                    View Evidence
                  </button>
                ) : null}
              </div>
              <ExtractionPanel
                actionNote={noteDraft}
                checklist={selectedChecklist}
                deleteMutationDocumentId={deleteMutationDocumentId}
                fieldMutationId={fieldMutationId}
                onChecklistChange={handleChecklistChange}
                onDeleteDocument={handleDeleteDocument}
                onFieldCorrection={handleFieldCorrection}
                onNoteChange={setNoteDraft}
                onOpenEvidence={handleOpenEvidence}
                onReparseDocument={handleReparseDocument}
                onReviewAction={handleReviewAction}
                reparseMutationDocumentId={reparseMutationDocumentId}
                reviewMutationDocumentId={reviewMutationDocumentId}
                selectedDocument={selectedDocument}
              />
            </article>

            <article className="quartz-card" id="source-intake">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-section-title">Source Intake & Evidence</h2>
              </div>
              <DocumentUploadPanel
                closeRunId={closeRunId}
                entityId={entityId}
                onUploadComplete={async () => {
                  await refreshWorkspace();
                }}
                pendingParseCount={pendingParseCount}
              />
              <div className="quartz-divider quartz-section" />
              <EvidenceDrawer
                emptyMessage="Select a field or queue row to open source-backed evidence references."
                isOpen={evidenceDrawer.isOpen}
                onClose={() => setEvidenceDrawer(defaultEvidenceDrawerState)}
                references={evidenceDrawer.references}
                sourceLabel={evidenceDrawer.sourceLabel}
                title={evidenceDrawer.title}
              />
            </article>
          </div>
        </section>
        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card ai">
              <p className="quartz-card-eyebrow secondary">Collection outlook</p>
              <h3>Inputs are nearing accounting readiness</h3>
              <p className="form-helper">
                {readyItemsCount} items are ready to move into recommendations and journals.{" "}
                {workspaceData.queueCounts.blocked} remain blocked and{" "}
                {workspaceData.queueCounts.wrong_period} are outside the target period.
              </p>
              <div className="quartz-mini-list">
                <div className="quartz-mini-item">
                  <strong>
                    {workspaceData.queueCounts.low_confidence} low-confidence captures
                  </strong>
                  <span className="quartz-mini-meta">
                    These fields still need accountant verification before downstream use.
                  </span>
                </div>
                <div className="quartz-mini-item">
                  <strong>{workspaceData.queueCounts.duplicate} suspected duplicates</strong>
                  <span className="quartz-mini-meta">
                    Remove duplicate evidence before the workflow advances.
                  </span>
                </div>
              </div>
            </article>

            <article className="quartz-card">
              <p className="quartz-card-eyebrow">Current focus</p>
              <h3>{selectedDocument?.originalFilename ?? "Select a document"}</h3>
              <p className="form-helper">
                {selectedDocument
                  ? (selectedDocument.primaryIssueReason ??
                    "This document is ready for accountant review.")
                  : "Choose a document row to inspect evidence, corrections, and review decisions."}
              </p>
              {selectedDocument ? (
                <div className="quartz-button-row">
                  <button
                    className="secondary-button"
                    onClick={() => handleOpenEvidenceForDocument(selectedDocument.id)}
                    type="button"
                  >
                    Open Evidence
                  </button>
                </div>
              ) : null}
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}

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

function deriveVerificationChecklistDraft(
  document: DocumentReviewWorkspaceData["items"][number] | null,
): DocumentVerificationChecklist {
  if (document === null) {
    return defaultVerificationChecklist;
  }

  if (document.status === "approved") {
    return {
      authorized: true,
      complete: true,
      period: true,
    };
  }

  return {
    ...defaultVerificationChecklist,
  };
}

function resolveDocumentStatusTone(
  item: Readonly<DocumentReviewQueueItem>,
): "error" | "neutral" | "success" | "warning" {
  if (item.issueSeverity === "blocking") {
    return "error";
  }
  if (item.hasException) {
    return "warning";
  }
  if (item.status === "approved") {
    return "success";
  }
  return "neutral";
}

function resolveDocumentStatusLabel(item: Readonly<DocumentReviewQueueItem>): string {
  if (item.issueTypes.length > 0) {
    return formatLabel(item.issueTypes[0] ?? item.status);
  }

  switch (item.status) {
    case "uploaded":
      return "Queued";
    case "processing":
      return "Processing";
    case "approved":
      return "Ready";
    case "failed":
      return "Failed";
    default:
      return formatLabel(item.status);
  }
}

function extractDocumentAmount(item: Readonly<DocumentReviewQueueItem>): string | null {
  const candidateField = item.extractedFields.find((field) =>
    ["amount", "total", "gross", "net"].some((keyword) =>
      `${field.fieldName} ${field.label}`.toLowerCase().includes(keyword),
    ),
  );

  if (candidateField === undefined) {
    return item.latestExtraction?.autoTransactionMatch?.matchedAmount ?? null;
  }

  const candidateValue =
    typeof candidateField.rawValue === "number"
      ? candidateField.rawValue
      : parseNumber(candidateField.value);

  if (candidateValue === null) {
    return candidateField.value;
  }

  return new Intl.NumberFormat("en-NG", {
    maximumFractionDigits: 2,
    minimumFractionDigits: 2,
  }).format(candidateValue);
}

function parseNumber(value: string): number | null {
  const normalized = value.replaceAll(",", "").replace(/[^\d.-]/gu, "");
  if (normalized.trim().length === 0) {
    return null;
  }

  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatLabel(value: string): string {
  return value
    .split("_")
    .map((part) => part.slice(0, 1).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatDocumentDate(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
  }).format(new Date(value));
}

function formatSourceChannel(value: DocumentReviewQueueItem["sourceChannel"]): string {
  switch (value) {
    case "api_import":
      return "API Import";
    case "manual_entry":
      return "Manual";
    case "upload":
      return "Upload";
  }
}
