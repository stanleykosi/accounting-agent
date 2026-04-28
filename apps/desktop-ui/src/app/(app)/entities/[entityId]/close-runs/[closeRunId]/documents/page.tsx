/*
Purpose: Render the minimal Quartz document workspace for one close run.
Scope: Queue loading, overlay-based source upload, inline table review actions, and compact detail expansion.
Dependencies: Document review API helpers, shared Quartz styles, and the close-run document workspace contract.
*/

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../../../../../../../components/layout/QuartzIcons";
import {
  deleteSourceDocument,
  type DocumentReviewFilter,
  type DocumentReviewQueueItem,
  type DocumentReviewWorkspaceData,
  DocumentReviewApiError,
  filterDocumentReviewItems,
  formatPeriodLabel,
  persistDocumentReviewDecision,
  queueUploadedDocumentsForParsing,
  readDocumentReviewWorkspace,
  reparseSourceDocument,
  uploadSourceDocuments,
} from "../../../../../../../lib/documents";
import { requireRouteParam } from "../../../../../../../lib/route-params";

type QueueFilterDefinition = {
  filter: DocumentReviewFilter;
  label: string;
};

type PendingDocumentAction =
  | {
      action: "delete";
      documentId: string;
      title: string;
      description: string;
      confirmLabel: string;
    }
  | {
      action: "reparse";
      documentId: string;
      title: string;
      description: string;
      confirmLabel: string;
    };

const filterDefinitions: readonly QueueFilterDefinition[] = [
  { filter: "all", label: "All Documents" },
  { filter: "low_confidence", label: "Low Confidence" },
  { filter: "blocked", label: "Blocked" },
  { filter: "duplicate", label: "Duplicates" },
  { filter: "wrong_period", label: "Wrong Period" },
];

export default function CloseRunDocumentsPage(): ReactElement {
  const routeParams = useParams<{ closeRunId: string; entityId: string }>();
  const closeRunId = requireRouteParam(routeParams.closeRunId, "closeRunId");
  const entityId = requireRouteParam(routeParams.entityId, "entityId");

  const directoryInputRef = useRef<HTMLInputElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [activeFilter, setActiveFilter] = useState<DocumentReviewFilter>("all");
  const [deleteMutationDocumentId, setDeleteMutationDocumentId] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [expandedDocumentId, setExpandedDocumentId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isUploadDialogOpen, setIsUploadDialogOpen] = useState(false);
  const [isUploadingDocuments, setIsUploadingDocuments] = useState(false);
  const [operationMessage, setOperationMessage] = useState<string | null>(null);
  const [pendingDocumentAction, setPendingDocumentAction] = useState<PendingDocumentAction | null>(
    null,
  );
  const [reparseMutationDocumentId, setReparseMutationDocumentId] = useState<string | null>(null);
  const [reviewMutationDocumentId, setReviewMutationDocumentId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [uploadDialogErrorMessage, setUploadDialogErrorMessage] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<DocumentReviewWorkspaceData | null>(null);

  const setDirectoryPickerRef = useCallback((node: HTMLInputElement | null): void => {
    directoryInputRef.current = node;
    if (node !== null) {
      node.setAttribute("webkitdirectory", "");
      node.setAttribute("directory", "");
    }
  }, []);

  const refreshWorkspace = useCallback(
    async (options?: Readonly<{ showLoading?: boolean }>): Promise<void> => {
      const showLoading = options?.showLoading ?? true;
      if (showLoading) {
        setIsLoading(true);
      }
      setErrorMessage(null);

      try {
        const nextWorkspace = await readDocumentReviewWorkspace(entityId, closeRunId);
        setWorkspaceData(nextWorkspace);
        setExpandedDocumentId((currentExpandedDocumentId) =>
          currentExpandedDocumentId !== null &&
          nextWorkspace.items.some((item) => item.id === currentExpandedDocumentId)
            ? currentExpandedDocumentId
            : null,
        );
      } catch (error: unknown) {
        if (error instanceof DocumentReviewApiError) {
          setErrorMessage(error.message);
        } else {
          setErrorMessage("Failed to load the document review queue. Reload and try again.");
        }
      } finally {
        if (showLoading) {
          setIsLoading(false);
        }
      }
    },
    [closeRunId, entityId],
  );

  useEffect(() => {
    void refreshWorkspace({ showLoading: true });
  }, [refreshWorkspace]);

  useEffect(() => {
    if (operationMessage === null) {
      return;
    }

    const timeoutId = window.setTimeout(() => {
      setOperationMessage(null);
    }, 3200);

    return () => {
      window.clearTimeout(timeoutId);
    };
  }, [operationMessage]);

  useEffect(() => {
    if (!isUploadDialogOpen) {
      return;
    }

    function handleEscape(event: KeyboardEvent): void {
      if (event.key === "Escape" && !isUploadingDocuments) {
        setIsUploadDialogOpen(false);
      }
    }

    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("keydown", handleEscape);
    };
  }, [isUploadDialogOpen, isUploadingDocuments]);

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

  const closeRunPeriodLabel =
    workspaceData === null
      ? null
      : formatPeriodLabel(workspaceData.closeRunPeriodStart, workspaceData.closeRunPeriodEnd);

  const handleApproveDocument = useCallback(
    async (documentId: string): Promise<void> => {
      setReviewMutationDocumentId(documentId);
      setOperationMessage(null);

      try {
        const result = await persistDocumentReviewDecision(
          entityId,
          closeRunId,
          documentId,
          "approved",
          undefined,
          {
            authorized: true,
            complete: true,
            period: true,
          },
        );
        setWorkspaceData((currentWorkspace) =>
          replaceWorkspaceDocument(currentWorkspace, result.document),
        );
        setOperationMessage("Document approved.");
      } catch (error: unknown) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setReviewMutationDocumentId(null);
      }
    },
    [closeRunId, entityId],
  );

  const handleRejectDocument = useCallback(
    async (documentId: string): Promise<void> => {
      setReviewMutationDocumentId(documentId);
      setOperationMessage(null);

      try {
        const result = await persistDocumentReviewDecision(
          entityId,
          closeRunId,
          documentId,
          "rejected",
        );
        setWorkspaceData((currentWorkspace) =>
          replaceWorkspaceDocument(currentWorkspace, result.document),
        );
        setOperationMessage("Document rejected.");
      } catch (error: unknown) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setReviewMutationDocumentId(null);
      }
    },
    [closeRunId, entityId],
  );

  const handleDeleteDocument = useCallback(
    (documentId: string): void => {
      const document =
        workspaceData?.items.find((candidate) => candidate.id === documentId) ?? null;
      if (document === null) {
        setErrorMessage("Select a document from the table before deleting it.");
        return;
      }

      setPendingDocumentAction({
        action: "delete",
        confirmLabel: "Delete document",
        description:
          `Delete ${document.originalFilename} from this close run? This removes the uploaded ` +
          "file and linked extraction data.",
        documentId,
        title: "Delete document",
      });
    },
    [workspaceData],
  );

  const handleReparseDocument = useCallback(
    (documentId: string): void => {
      const document =
        workspaceData?.items.find((candidate) => candidate.id === documentId) ?? null;
      if (document === null) {
        setErrorMessage("Select a document from the table before reparsing it.");
        return;
      }

      setPendingDocumentAction({
        action: "reparse",
        confirmLabel: "Reparse document",
        description:
          `Reparse ${document.originalFilename}? This clears the current extraction and queues ` +
          "a fresh parse.",
        documentId,
        title: "Reparse document",
      });
    },
    [workspaceData],
  );

  const handleUploadSelection = useCallback(
    async (candidateFiles: readonly File[]): Promise<void> => {
      const { supportedFiles, unsupportedCount } = partitionSupportedFiles(candidateFiles);

      if (supportedFiles.length === 0) {
        setUploadDialogErrorMessage(
          unsupportedCount > 0
            ? "Only PDF, CSV, and Excel files can be uploaded."
            : "Choose at least one PDF, CSV, or Excel file to upload.",
        );
        return;
      }

      setIsUploadingDocuments(true);
      setUploadDialogErrorMessage(null);
      setOperationMessage(null);

      try {
        const uploadResult = await uploadSourceDocuments(entityId, closeRunId, supportedFiles);

        try {
          const parseResult = await queueUploadedDocumentsForParsing(entityId, closeRunId);
          if (fileInputRef.current !== null) {
            fileInputRef.current.value = "";
          }
          if (directoryInputRef.current !== null) {
            directoryInputRef.current.value = "";
          }
          setIsUploadDialogOpen(false);
          setOperationMessage(
            buildUploadOperationMessage(
              uploadResult.uploadedCount,
              parseResult.queuedCount,
              unsupportedCount,
            ),
          );
          await refreshWorkspace({ showLoading: false });
        } catch (error: unknown) {
          await refreshWorkspace({ showLoading: false });
          setUploadDialogErrorMessage(
            uploadResult.uploadedCount === 1
              ? "The document uploaded, but parsing could not start automatically. Retry the upload."
              : "The documents uploaded, but parsing could not start automatically. Retry the upload.",
          );
          setErrorMessage(resolveDocumentReviewErrorMessage(error));
        }
      } catch (error: unknown) {
        setUploadDialogErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setIsUploadingDocuments(false);
      }
    },
    [closeRunId, entityId, refreshWorkspace],
  );

  const handleUploadInputChange = (event: ChangeEvent<HTMLInputElement>): void => {
    const selectedFiles = Array.from(event.target.files ?? []);
    event.target.value = "";
    void handleUploadSelection(selectedFiles);
  };

  const handleToggleDocumentDetails = (documentId: string): void => {
    setExpandedDocumentId((currentDocumentId) =>
      currentDocumentId === documentId ? null : documentId,
    );
  };

  const openUploadDialog = (): void => {
    setUploadDialogErrorMessage(null);
    setIsUploadDialogOpen(true);
  };

  const closeUploadDialog = (): void => {
    if (isUploadingDocuments) {
      return;
    }

    setIsUploadDialogOpen(false);
    setUploadDialogErrorMessage(null);
  };

  const closePendingDocumentAction = (): void => {
    if (
      pendingDocumentAction !== null &&
      (deleteMutationDocumentId === pendingDocumentAction.documentId ||
        reparseMutationDocumentId === pendingDocumentAction.documentId)
    ) {
      return;
    }

    setPendingDocumentAction(null);
  };

  const confirmPendingDocumentAction = async (): Promise<void> => {
    if (pendingDocumentAction === null) {
      return;
    }

    setOperationMessage(null);
    setErrorMessage(null);

    if (pendingDocumentAction.action === "delete") {
      setDeleteMutationDocumentId(pendingDocumentAction.documentId);
      try {
        await deleteSourceDocument(entityId, closeRunId, pendingDocumentAction.documentId);
        setWorkspaceData((currentWorkspace) =>
          removeWorkspaceDocument(currentWorkspace, pendingDocumentAction.documentId),
        );
        setPendingDocumentAction(null);
        setOperationMessage("Document deleted.");
      } catch (error: unknown) {
        setErrorMessage(resolveDocumentReviewErrorMessage(error));
      } finally {
        setDeleteMutationDocumentId(null);
      }
      return;
    }

    setReparseMutationDocumentId(pendingDocumentAction.documentId);
    try {
      await reparseSourceDocument(entityId, closeRunId, pendingDocumentAction.documentId);
      setWorkspaceData((currentWorkspace) =>
        markWorkspaceDocumentReparsing(currentWorkspace, pendingDocumentAction.documentId),
      );
      setPendingDocumentAction(null);
      setOperationMessage("Document queued for reparsing.");
      void refreshWorkspace({ showLoading: false });
    } catch (error: unknown) {
      setErrorMessage(resolveDocumentReviewErrorMessage(error));
    } finally {
      setReparseMutationDocumentId(null);
    }
  };

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading document workspace...</div>
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
              "The document workspace could not be loaded. Verify the entity and close-run IDs, then retry."}
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
            <h1>Document Workspace</h1>
            <p className="quartz-page-subtitle">
              Review uploaded source documents for {closeRunPeriodLabel}. Files begin parsing
              automatically after upload.
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
                placeholder="Search documents"
                type="search"
                value={searchQuery}
              />
            </label>
            <Link
              className="secondary-button quartz-toolbar-button"
              href={`/entities/${entityId}/close-runs/${closeRunId}/chat`}
            >
              <QuartzIcon className="quartz-inline-icon" name="assistant" />
              Open Assistant
            </Link>
            <button
              className="primary-button quartz-toolbar-button"
              onClick={openUploadDialog}
              type="button"
            >
              <QuartzIcon className="quartz-inline-icon" name="upload" />
              Upload Documents
            </button>
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

          <div className="quartz-table-shell quartz-document-table-shell">
            <table className="quartz-table quartz-document-table">
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Uploaded</th>
                  <th>Amount (NGN)</th>
                  <th>Status</th>
                  <th className="quartz-table-center">Actions</th>
                </tr>
              </thead>
              <tbody>
                {visibleItems.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="quartz-empty-state">
                        No documents match the current search and filter combination.
                      </div>
                    </td>
                  </tr>
                ) : (
                  visibleItems.map((item) => {
                    const amount = extractDocumentAmount(item);
                    const isBusy =
                      reviewMutationDocumentId === item.id ||
                      reparseMutationDocumentId === item.id ||
                      deleteMutationDocumentId === item.id;
                    const isExpanded = expandedDocumentId === item.id;
                    const statusTone = resolveDocumentStatusTone(item);

                    return (
                      <Fragment key={item.id}>
                        <tr
                          className={[
                            item.hasException ? "quartz-table-row error" : "",
                            isExpanded ? "quartz-table-row selected" : "",
                          ]
                            .filter(Boolean)
                            .join(" ")}
                        >
                          <td>
                            <div className="quartz-table-primary">{item.originalFilename}</div>
                            <div className="quartz-table-secondary">
                              {item.primaryIssueReason ?? "Ready for accountant review"}
                            </div>
                          </td>
                          <td>{formatDocumentDate(item.createdAt)}</td>
                          <td className="quartz-table-numeric">{amount ?? "-"}</td>
                          <td>
                            <span className={`quartz-status-badge ${statusTone}`}>
                              {resolveDocumentStatusLabel(item)}
                            </span>
                          </td>
                          <td className="quartz-table-center">
                            <div className="quartz-table-icon-actions">
                              <button
                                aria-label={
                                  isExpanded ? "Hide document details" : "Show document details"
                                }
                                className={`quartz-table-icon-action ${isExpanded ? "active" : ""}`}
                                disabled={isBusy}
                                onClick={() => handleToggleDocumentDetails(item.id)}
                                title={isExpanded ? "Hide details" : "Show details"}
                                type="button"
                              >
                                <QuartzIcon className="quartz-inline-icon" name="help" />
                              </button>
                              <button
                                aria-label="Approve document"
                                className="quartz-table-icon-action success"
                                disabled={
                                  isBusy ||
                                  item.status === "uploaded" ||
                                  item.status === "processing" ||
                                  item.status === "failed"
                                }
                                onClick={() => {
                                  void handleApproveDocument(item.id);
                                }}
                                title="Approve document"
                                type="button"
                              >
                                <QuartzIcon className="quartz-inline-icon" name="check" />
                              </button>
                              <button
                                aria-label="Reject document"
                                className="quartz-table-icon-action danger"
                                disabled={isBusy}
                                onClick={() => {
                                  void handleRejectDocument(item.id);
                                }}
                                title="Reject document"
                                type="button"
                              >
                                <QuartzIcon className="quartz-inline-icon" name="dismiss" />
                              </button>
                              <button
                                aria-label="Reparse document"
                                className="quartz-table-icon-action"
                                disabled={isBusy}
                                onClick={() => {
                                  void handleReparseDocument(item.id);
                                }}
                                title="Reparse document"
                                type="button"
                              >
                                <QuartzIcon className="quartz-inline-icon" name="refresh" />
                              </button>
                              <button
                                aria-label="Delete document"
                                className="quartz-table-icon-action danger"
                                disabled={isBusy}
                                onClick={() => {
                                  void handleDeleteDocument(item.id);
                                }}
                                title="Delete document"
                                type="button"
                              >
                                <QuartzIcon className="quartz-inline-icon" name="trash" />
                              </button>
                            </div>
                          </td>
                        </tr>
                        {isExpanded ? (
                          <tr className="quartz-document-detail-row">
                            <td colSpan={5}>
                              <div className="quartz-document-detail-panel">
                                <div className="quartz-document-detail-grid">
                                  <article className="quartz-document-detail-card">
                                    <span>Detected Type</span>
                                    <strong>{formatLabel(item.documentType)}</strong>
                                  </article>
                                  <article className="quartz-document-detail-card">
                                    <span>Confidence</span>
                                    <strong>
                                      {formatConfidenceSummary(
                                        item.confidenceBand,
                                        item.classificationConfidence,
                                      )}
                                    </strong>
                                  </article>
                                  <article className="quartz-document-detail-card">
                                    <span>Period</span>
                                    <strong>{formatPeriodState(item.periodState)}</strong>
                                  </article>
                                  <article className="quartz-document-detail-card">
                                    <span>Extraction</span>
                                    <strong>
                                      {item.latestExtraction
                                        ? `v${item.latestExtraction.versionNo} ${
                                            item.latestExtraction.approvedVersion
                                              ? "approved"
                                              : "pending"
                                          }`
                                        : "Not extracted"}
                                    </strong>
                                  </article>
                                </div>

                                {item.openIssues.length > 0 ? (
                                  <div className="quartz-document-issue-stack">
                                    {item.openIssues.map((issue) => (
                                      <article
                                        className={`quartz-document-issue-item ${
                                          issue.severity === "blocking" ? "blocking" : "warning"
                                        }`}
                                        key={issue.id}
                                      >
                                        <strong>{formatLabel(issue.issueType)}</strong>
                                        <span>
                                          {typeof issue.details.reason === "string"
                                            ? issue.details.reason
                                            : `${formatLabel(issue.severity)} • ${formatLabel(issue.status)}`}
                                        </span>
                                      </article>
                                    ))}
                                  </div>
                                ) : (
                                  <div className="quartz-inline-note">
                                    No active review findings. Approve when the document is ready.
                                  </div>
                                )}
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </section>

      {isUploadDialogOpen ? (
        <div
          aria-modal="true"
          className="quartz-modal-backdrop"
          onClick={closeUploadDialog}
          role="dialog"
        >
          <div
            className="quartz-modal-card quartz-document-upload-modal"
            onClick={(event) => event.stopPropagation()}
            role="document"
          >
            <div className="quartz-section-header quartz-section-header-tight">
              <div>
                <h2 className="quartz-section-title">Upload Documents</h2>
                <p className="quartz-page-subtitle">
                  Select individual files or a whole folder. Upload and parsing begin immediately.
                </p>
              </div>
              <button
                aria-label="Close"
                className="quartz-icon-button"
                onClick={closeUploadDialog}
                type="button"
              >
                <QuartzIcon name="dismiss" />
              </button>
            </div>

            <div className="quartz-upload-choice-grid">
              <button
                className="quartz-upload-choice"
                disabled={isUploadingDocuments}
                onClick={() => fileInputRef.current?.click()}
                type="button"
              >
                <QuartzIcon className="quartz-upload-choice-icon" name="upload" />
                <strong>Select Files</strong>
                <span>Upload multiple PDFs, CSVs, or Excel files in one batch.</span>
              </button>
              <button
                className="quartz-upload-choice"
                disabled={isUploadingDocuments}
                onClick={() => directoryInputRef.current?.click()}
                type="button"
              >
                <QuartzIcon className="quartz-upload-choice-icon" name="folder" />
                <strong>Select Folder</strong>
                <span>Choose a folder and upload every supported document inside it.</span>
              </button>
            </div>

            <input
              accept=".pdf,.csv,.xlsx,.xls,.xlsm"
              className="sr-only"
              hidden
              multiple
              onChange={handleUploadInputChange}
              ref={fileInputRef}
              tabIndex={-1}
              type="file"
            />
            <input
              className="sr-only"
              hidden
              multiple
              onChange={handleUploadInputChange}
              ref={setDirectoryPickerRef}
              tabIndex={-1}
              type="file"
            />

            {isUploadingDocuments ? (
              <div className="quartz-inline-note" role="status">
                Uploading documents and starting parsing...
              </div>
            ) : null}

            {uploadDialogErrorMessage ? (
              <div className="status-banner warning" role="status">
                {uploadDialogErrorMessage}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {pendingDocumentAction ? (
        <div
          aria-modal="true"
          className="quartz-modal-backdrop"
          onClick={closePendingDocumentAction}
          role="dialog"
        >
          <div
            className="quartz-modal-card"
            onClick={(event) => event.stopPropagation()}
            role="document"
          >
            <div className="quartz-section-header quartz-section-header-tight">
              <div>
                <h2 className="quartz-section-title">{pendingDocumentAction.title}</h2>
                <p className="quartz-page-subtitle">{pendingDocumentAction.description}</p>
              </div>
              <button
                aria-label="Close"
                className="quartz-icon-button"
                onClick={closePendingDocumentAction}
                type="button"
              >
                <QuartzIcon name="dismiss" />
              </button>
            </div>

            <div className="quartz-form-row quartz-modal-actions">
              <button
                className="secondary-button"
                onClick={closePendingDocumentAction}
                type="button"
              >
                Cancel
              </button>
              <button
                className="primary-button"
                disabled={
                  deleteMutationDocumentId === pendingDocumentAction.documentId ||
                  reparseMutationDocumentId === pendingDocumentAction.documentId
                }
                onClick={() => {
                  void confirmPendingDocumentAction();
                }}
                type="button"
              >
                {deleteMutationDocumentId === pendingDocumentAction.documentId ||
                reparseMutationDocumentId === pendingDocumentAction.documentId
                  ? "Saving..."
                  : pendingDocumentAction.confirmLabel}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function buildUploadOperationMessage(
  uploadedCount: number,
  queuedCount: number,
  unsupportedCount: number,
): string {
  const uploadLabel =
    uploadedCount === 1 ? "1 document uploaded" : `${uploadedCount} documents uploaded`;
  const parseLabel =
    queuedCount === 1
      ? "1 document queued for parsing"
      : `${queuedCount} documents queued for parsing`;
  const unsupportedLabel =
    unsupportedCount === 0
      ? ""
      : unsupportedCount === 1
        ? " 1 unsupported file was skipped."
        : ` ${unsupportedCount} unsupported files were skipped.`;
  return `${uploadLabel}; ${parseLabel}.${unsupportedLabel}`;
}

function replaceWorkspaceDocument(
  workspace: Readonly<DocumentReviewWorkspaceData> | null,
  nextDocument: Readonly<DocumentReviewQueueItem>,
): DocumentReviewWorkspaceData | null {
  if (workspace === null) {
    return null;
  }

  return rebuildWorkspaceData(
    workspace,
    workspace.items.map((item) => (item.id === nextDocument.id ? nextDocument : item)),
  );
}

function removeWorkspaceDocument(
  workspace: Readonly<DocumentReviewWorkspaceData> | null,
  documentId: string,
): DocumentReviewWorkspaceData | null {
  if (workspace === null) {
    return null;
  }

  return rebuildWorkspaceData(
    workspace,
    workspace.items.filter((item) => item.id !== documentId),
  );
}

function markWorkspaceDocumentReparsing(
  workspace: Readonly<DocumentReviewWorkspaceData> | null,
  documentId: string,
): DocumentReviewWorkspaceData | null {
  if (workspace === null) {
    return null;
  }

  const now = new Date().toISOString();
  return rebuildWorkspaceData(
    workspace,
    workspace.items.map((item) =>
      item.id === documentId
        ? {
            ...item,
            extractedFields: [],
            hasException: false,
            issueSeverity: null,
            issueTypes: [],
            latestExtraction: null,
            openIssues: [],
            primaryIssueReason: "Reparsing in progress",
            status: "processing",
            updatedAt: now,
          }
        : item,
    ),
  );
}

function rebuildWorkspaceData(
  workspace: Readonly<DocumentReviewWorkspaceData>,
  items: readonly DocumentReviewQueueItem[],
): DocumentReviewWorkspaceData {
  return {
    ...workspace,
    items,
    queueCounts: buildWorkspaceQueueCounts(items),
  };
}

function buildWorkspaceQueueCounts(
  items: readonly DocumentReviewQueueItem[],
): DocumentReviewWorkspaceData["queueCounts"] {
  return {
    all: items.length,
    blocked: filterDocumentReviewItems(items, "blocked").length,
    duplicate: filterDocumentReviewItems(items, "duplicate").length,
    low_confidence: filterDocumentReviewItems(items, "low_confidence").length,
    wrong_period: filterDocumentReviewItems(items, "wrong_period").length,
  };
}

function resolveDocumentReviewErrorMessage(error: unknown): string {
  if (error instanceof DocumentReviewApiError) {
    if (error.code === "workflow_phase_locked") {
      return "Document actions are only available during Collection. Return the close run to Collection before continuing.";
    }

    return error.message;
  }

  return "The requested document action could not be completed. Retry after refreshing the workspace.";
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

function partitionSupportedFiles(files: readonly File[]): {
  supportedFiles: readonly File[];
  unsupportedCount: number;
} {
  const supportedExtensions = new Set(["csv", "pdf", "xls", "xlsm", "xlsx"]);
  const supportedFiles: File[] = [];
  let unsupportedCount = 0;

  for (const file of files) {
    const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
    if (!supportedExtensions.has(extension)) {
      unsupportedCount += 1;
      continue;
    }
    supportedFiles.push(file);
  }

  return { supportedFiles, unsupportedCount };
}

function parseNumber(value: string): number | null {
  const normalized = value.replaceAll(",", "").replace(/[^\d.-]/gu, "");
  if (normalized.trim().length === 0) {
    return null;
  }

  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatConfidenceSummary(
  band: DocumentReviewQueueItem["confidenceBand"],
  confidence: number | null,
): string {
  const label = formatLabel(band);
  if (confidence === null) {
    return label;
  }
  return `${label} (${Math.round(confidence * 100)}%)`;
}

function formatPeriodState(value: DocumentReviewQueueItem["periodState"]): string {
  if (value === "in_period") {
    return "Within close-run period";
  }
  if (value === "out_of_period") {
    return "Outside close-run period";
  }
  return "Period not detected";
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
