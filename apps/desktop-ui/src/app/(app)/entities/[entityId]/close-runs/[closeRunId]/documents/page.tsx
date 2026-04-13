/*
Purpose: Render the close-run document review queue and evidence-first exception workspace.
Scope: Queue loading, filter and selection state, side-by-side extraction context, and evidence drawer coordination.
Dependencies: Document review API helpers, queue/detail components, and shared UI surface/evidence primitives.
*/

"use client";

import { EvidenceDrawer, ReviewLayout, SurfaceCard } from "@accounting-ai-agent/ui";
import { use, useEffect, useMemo, useState, type ReactElement } from "react";
import { DocumentReviewTable } from "../../../../../../../components/documents/DocumentReviewTable";
import { ExtractionPanel } from "../../../../../../../components/documents/ExtractionPanel";
import {
  DocumentReviewApiError,
  filterDocumentReviewItems,
  formatPeriodLabel,
  readDocumentReviewWorkspace,
  type DocumentReviewFilter,
  type DocumentReviewWorkspaceData,
  type EvidenceReference,
  type ReviewDraftDecision,
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
 * Behavior: Loads queue state from same-origin API routes and keeps reviewer decisions local to the active page session.
 */
export default function CloseRunDocumentsPage({
  params,
}: Readonly<CloseRunDocumentsPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);

  const [activeFilter, setActiveFilter] = useState<DocumentReviewFilter>("all");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [reviewDecisions, setReviewDecisions] = useState<
    Readonly<Record<string, ReviewDraftDecision | undefined>>
  >({});
  const [selectedDocumentId, setSelectedDocumentId] = useState<string | null>(null);
  const [workspaceData, setWorkspaceData] = useState<DocumentReviewWorkspaceData | null>(null);
  const [evidenceDrawer, setEvidenceDrawer] = useState<EvidenceDrawerState>(defaultEvidenceDrawerState);

  useEffect(() => {
    void loadWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: (nextWorkspace) => {
        setWorkspaceData(nextWorkspace);
        setSelectedDocumentId(selectInitialDocumentId(nextWorkspace));
      },
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

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

  const selectedDraftDecision =
    selectedDocument !== null ? (reviewDecisions[selectedDocument.id] ?? null) : null;

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

  const handleReviewAction = (documentId: string, decision: ReviewDraftDecision): void => {
    setReviewDecisions((currentState) => ({
      ...currentState,
      [documentId]: decision,
    }));
  };

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

      <ReviewLayout
        className="document-review-grid"
        main={
          <SurfaceCard title="Exception Queue" subtitle="Review table">
            <DocumentReviewTable
              activeFilter={activeFilter}
              items={visibleItems}
              onFilterChange={handleFilterChange}
              onOpenEvidence={handleOpenEvidenceForDocument}
              onReviewAction={handleReviewAction}
              onSelectDocument={setSelectedDocumentId}
              queueCounts={workspaceData.queueCounts}
              reviewDecisions={reviewDecisions}
              selectedDocumentId={selectedDocumentId}
            />
          </SurfaceCard>
        }
        side={
          <div className="document-review-side-column">
            <SurfaceCard title="Extraction Context" subtitle="Selected document">
              <ExtractionPanel
                draftDecision={selectedDraftDecision}
                onOpenEvidence={handleOpenEvidence}
                onReviewAction={handleReviewAction}
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
