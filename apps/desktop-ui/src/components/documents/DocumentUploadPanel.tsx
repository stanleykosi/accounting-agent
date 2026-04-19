/*
Purpose: Let reviewers add new source documents through the canonical hosted upload flow.
Scope: File selection, upload submission, status messaging, and post-upload refresh callbacks.
Dependencies: React client hooks and the document upload helper in the shared documents lib.
*/

"use client";

import { useCallback, useRef, useState, type ChangeEvent, type ReactElement } from "react";
import {
  DocumentReviewApiError,
  queueUploadedDocumentsForParsing,
  uploadSourceDocuments,
} from "../../lib/documents";

type DocumentUploadPanelProps = {
  closeRunId: string;
  entityId: string;
  onUploadComplete: (uploadedCount: number) => Promise<void> | void;
  pendingParseCount: number;
};

/**
 * Purpose: Provide one canonical source-document upload surface for hosted browser and Tauri shells.
 * Inputs: Entity/close-run identifiers plus a refresh callback after successful finalization.
 * Outputs: A compact upload panel with explicit error messaging and queue refresh after upload.
 * Behavior: Sends files to the backend API, which stores them first and waits for explicit parse queueing.
 */
export function DocumentUploadPanel({
  closeRunId,
  entityId,
  onUploadComplete,
  pendingParseCount,
}: Readonly<DocumentUploadPanelProps>): ReactElement {
const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isQueueingParse, setIsQueueingParse] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [selectedFiles, setSelectedFiles] = useState<readonly File[]>([]);
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const directoryInputRef = useRef<HTMLInputElement | null>(null);

  const setDirectoryPickerRef = useCallback((node: HTMLInputElement | null): void => {
    directoryInputRef.current = node;
    if (node !== null) {
      node.setAttribute("webkitdirectory", "");
      node.setAttribute("directory", "");
    }
  }, []);

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    const { supportedFiles, unsupportedCount } = partitionSupportedFiles(
      Array.from(event.target.files ?? []),
    );
    setSelectedFiles((currentFiles) => mergeSelectedFiles(currentFiles, supportedFiles));
    setErrorMessage(
      unsupportedCount > 0
        ? `${unsupportedCount} unsupported file${unsupportedCount === 1 ? "" : "s"} were skipped. Only PDF, CSV, and Excel files can be uploaded.`
        : null,
    );
    setSuccessMessage(null);
    event.target.value = "";
  };

  const handleUpload = async (): Promise<void> => {
    if (selectedFiles.length === 0) {
      setErrorMessage("Choose at least one PDF, CSV, or Excel file to upload.");
      return;
    }

    setIsUploading(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    try {
      const result = await uploadSourceDocuments(entityId, closeRunId, selectedFiles);
      setSelectedFiles([]);
      if (fileInputRef.current !== null) {
        fileInputRef.current.value = "";
      }
      if (directoryInputRef.current !== null) {
        directoryInputRef.current.value = "";
      }
      setSuccessMessage(
        result.uploadedCount === 1
          ? "1 source document uploaded and staged. Review the queue, then proceed to parsing."
          : `${result.uploadedCount} source documents uploaded and staged. Review the queue, then proceed to parsing.`,
      );
      await onUploadComplete(result.uploadedCount);
    } catch (error: unknown) {
      if (error instanceof DocumentReviewApiError) {
        setErrorMessage(error.message);
      } else if (error instanceof Error && error.message.trim().length > 0) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("The source documents could not be uploaded. Retry the batch.");
      }
    } finally {
      setIsUploading(false);
    }
  };

  const handleQueueParsing = async (): Promise<void> => {
    setIsQueueingParse(true);
    setErrorMessage(null);
    setSuccessMessage(null);
    try {
      const result = await queueUploadedDocumentsForParsing(entityId, closeRunId);
      setSuccessMessage(
        result.queuedCount === 1
          ? "1 uploaded document moved into parsing."
          : `${result.queuedCount} uploaded documents moved into parsing.`,
      );
      await onUploadComplete(0);
    } catch (error: unknown) {
      if (error instanceof DocumentReviewApiError) {
        setErrorMessage(error.message);
      } else if (error instanceof Error && error.message.trim().length > 0) {
        setErrorMessage(error.message);
      } else {
        setErrorMessage("The uploaded documents could not be queued for parsing. Retry the action.");
      }
    } finally {
      setIsQueueingParse(false);
    }
  };

  return (
    <div className="document-upload-panel">
      <div className="document-upload-panel-copy">
        <p className="eyebrow">Source Intake</p>
        <h2>Upload source documents.</h2>
        <p className="form-helper">
          Upload incrementally or choose the whole `source-documents` folder at once. Files land in
          the review queue first, and parsing starts only when you explicitly confirm the batch.
        </p>
      </div>

      <div className="document-upload-actions">
        <button
          className="secondary-button"
          onClick={() => fileInputRef.current?.click()}
          type="button"
        >
          Select files
        </button>
        <button
          className="secondary-button"
          onClick={() => directoryInputRef.current?.click()}
          type="button"
        >
          Select folder
        </button>
        {selectedFiles.length > 0 ? (
          <button
            className="secondary-button"
            onClick={() => {
              setSelectedFiles([]);
              setErrorMessage(null);
              setSuccessMessage(null);
            }}
            type="button"
          >
            Clear selection
          </button>
        ) : null}
      </div>

      <input
        accept=".pdf,.csv,.xlsx,.xls,.xlsm"
        className="sr-only"
        multiple
        onChange={handleFileChange}
        ref={fileInputRef}
        type="file"
      />
      <input
        className="sr-only"
        multiple
        onChange={handleFileChange}
        ref={setDirectoryPickerRef}
        type="file"
      />

      {selectedFiles.length > 0 ? (
        <div className="document-upload-file-list" role="list">
          {selectedFiles.map((file) => (
            <div className="document-upload-file" key={`${file.name}:${file.size}`} role="listitem">
              <strong>{file.name}</strong>
              <span>{formatByteSize(file.size)}</span>
            </div>
          ))}
        </div>
      ) : null}

      {successMessage ? (
        <div className="status-banner success" role="status">
          {successMessage}
        </div>
      ) : null}

      {errorMessage ? (
        <div className="status-banner danger" role="alert">
          {errorMessage}
        </div>
      ) : null}

      <div className="document-upload-actions">
        <button
          className="primary-button"
          disabled={isUploading || selectedFiles.length === 0}
          onClick={() => {
            void handleUpload();
          }}
          type="button"
        >
          {isUploading ? "Uploading..." : "Upload documents"}
        </button>
        <button
          className="secondary-button"
          disabled={isQueueingParse || pendingParseCount === 0}
          onClick={() => {
            void handleQueueParsing();
          }}
          type="button"
        >
          {isQueueingParse
            ? "Starting parsing..."
            : pendingParseCount === 0
              ? "Proceed to parsing"
              : `Proceed to parsing (${pendingParseCount})`}
        </button>
      </div>

      {pendingParseCount > 0 ? (
        <p className="form-helper">
          {pendingParseCount === 1
            ? "1 uploaded document is waiting in the queue."
            : `${pendingParseCount} uploaded documents are waiting in the queue.`}{" "}
          Delete mistakes before parsing if needed.
        </p>
      ) : null}
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

function mergeSelectedFiles(
  currentFiles: readonly File[],
  nextFiles: readonly File[],
): readonly File[] {
  const merged = new Map<string, File>();
  for (const file of [...currentFiles, ...nextFiles]) {
    merged.set(buildSelectedFileKey(file), file);
  }
  return Array.from(merged.values());
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

function buildSelectedFileKey(file: File): string {
  const relativePath =
    "webkitRelativePath" in file &&
    typeof file.webkitRelativePath === "string" &&
    file.webkitRelativePath.length > 0
      ? file.webkitRelativePath
      : file.name;
  return `${relativePath}:${file.size}:${file.lastModified}`;
}
