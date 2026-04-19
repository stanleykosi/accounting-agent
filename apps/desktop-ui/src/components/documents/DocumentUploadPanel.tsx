/*
Purpose: Let reviewers add new source documents through the canonical hosted upload flow.
Scope: File selection, upload submission, status messaging, and post-upload refresh callbacks.
Dependencies: React client hooks and the document upload helper in the shared documents lib.
*/

"use client";

import { useCallback, useRef, useState, type ChangeEvent, type ReactElement } from "react";
import { DocumentReviewApiError, uploadSourceDocuments } from "../../lib/documents";

type DocumentUploadPanelProps = {
  closeRunId: string;
  entityId: string;
  onUploadComplete: (uploadedCount: number) => Promise<void> | void;
};

/**
 * Purpose: Provide one canonical source-document upload surface for hosted browser and Tauri shells.
 * Inputs: Entity/close-run identifiers plus a refresh callback after successful finalization.
 * Outputs: A compact upload panel with explicit error messaging and queue refresh after upload.
 * Behavior: Sends files to the backend API, which stores them and enqueues parsing deterministically.
 */
export function DocumentUploadPanel({
  closeRunId,
  entityId,
  onUploadComplete,
}: Readonly<DocumentUploadPanelProps>): ReactElement {
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
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
    const nextFiles = Array.from(event.target.files ?? []);
    setSelectedFiles((currentFiles) => mergeSelectedFiles(currentFiles, nextFiles));
    setErrorMessage(null);
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
          ? "1 source document uploaded and queued for parsing."
          : `${result.uploadedCount} source documents uploaded and queued for parsing.`,
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

  return (
    <div className="document-upload-panel">
      <div className="document-upload-panel-copy">
        <p className="eyebrow">Source Intake</p>
        <h2>Upload source documents.</h2>
        <p className="form-helper">
          Upload incrementally or choose the whole `source-documents` folder at once. Missing bank
          statements do not block document review while you are still assembling the run.
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
        accept=".pdf,.csv,.xlsx,.xls,.xlsm"
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
      </div>
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
    merged.set(
      `${file.name}:${file.size}:${file.lastModified}`,
      file,
    );
  }
  return Array.from(merged.values());
}
