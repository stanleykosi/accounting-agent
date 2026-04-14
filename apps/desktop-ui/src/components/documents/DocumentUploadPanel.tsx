/*
Purpose: Let reviewers add new source documents through the canonical hosted upload flow.
Scope: File selection, upload submission, status messaging, and post-upload refresh callbacks.
Dependencies: React client hooks and the document upload helper in the shared documents lib.
*/

"use client";

import { useRef, useState, type ChangeEvent, type ReactElement } from "react";
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
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    const nextFiles = Array.from(event.target.files ?? []);
    setSelectedFiles(nextFiles);
    setErrorMessage(null);
    setSuccessMessage(null);
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
      if (inputRef.current !== null) {
        inputRef.current.value = "";
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
          Hosted browser and Tauri shells send files through the API, which stores the originals
          and queues parsing jobs for this close run.
        </p>
      </div>

      <label className="document-upload-input">
        <span>Select files</span>
        <input
          accept=".pdf,.csv,.xlsx,.xls,.xlsm"
          multiple
          onChange={handleFileChange}
          ref={inputRef}
          type="file"
        />
      </label>

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
