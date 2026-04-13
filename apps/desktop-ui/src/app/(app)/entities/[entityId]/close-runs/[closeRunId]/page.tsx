/*
Purpose: Render the close-run overview page with lifecycle status, phase progress, and activity context.
Scope: Close-run workspace loading, overview metrics, phase progression, quick links into review surfaces, and related period navigation.
Dependencies: Same-origin close-run/entity API helpers and shared desktop UI components.
*/

"use client";

import { PhaseProgress, SurfaceCard, Timeline, type TimelineItem } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { use, useEffect, useMemo, useState, type ReactElement } from "react";
import {
  CloseRunApiError,
  buildPhaseProgressItems,
  deriveCloseRunAttention,
  findActivePhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  readCloseRunWorkspace,
  type CloseRunWorkspaceData,
} from "../../../../../../lib/close-runs";
import { EntityApiError } from "../../../../../../lib/entities/api";

type CloseRunOverviewPageProps = {
  params: Promise<{
    closeRunId: string;
    entityId: string;
  }>;
};

/**
 * Purpose: Render the desktop overview surface for one entity close run.
 * Inputs: Route params containing the entity and close-run UUIDs.
 * Outputs: A client-rendered close-run overview with progress, activity, and queue-entry links.
 * Behavior: Hydrates the close-run, entity workspace, and sibling runs together so the page stays context-rich.
 */
export default function CloseRunOverviewPage({
  params,
}: Readonly<CloseRunOverviewPageProps>): ReactElement {
  const { closeRunId, entityId } = use(params);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [workspaceData, setWorkspaceData] = useState<CloseRunWorkspaceData | null>(null);

  useEffect(() => {
    void loadCloseRunWorkspace({
      closeRunId,
      entityId,
      onError: setErrorMessage,
      onLoaded: setWorkspaceData,
      onLoadingChange: setIsLoading,
    });
  }, [closeRunId, entityId]);

  const relatedCloseRuns = useMemo(
    () =>
      workspaceData === null
        ? []
        : workspaceData.closeRuns
            .filter((closeRun) => closeRun.id !== workspaceData.closeRun.id)
            .slice(0, 4),
    [workspaceData],
  );

  const timelineItems = useMemo<readonly TimelineItem[]>(() => {
    if (workspaceData === null) {
      return [];
    }

    const closeRun = workspaceData.closeRun;
    const items: Array<{
      badge: string;
      detail: string;
      id: string;
      occurredAt: string;
      title: string;
      tone: NonNullable<TimelineItem["tone"]>;
    }> = [
      {
        badge: workspaceData.entity.name,
        detail: `Close run version ${closeRun.currentVersionNo} opened for ${formatCloseRunPeriod(closeRun)}.`,
        id: `${closeRun.id}-opened`,
        occurredAt: closeRun.createdAt,
        title: "Close run opened",
        tone: "default",
      },
    ];

    closeRun.workflowState.phaseStates.forEach((phaseState) => {
      if (phaseState.completedAt === null) {
        return;
      }

      items.push({
        badge: phaseState.phase.replaceAll("_", " "),
        detail: "The workflow advanced after this phase completed.",
        id: `${closeRun.id}-${phaseState.phase}-completed`,
        occurredAt: phaseState.completedAt,
        title: `${phaseState.phase.replaceAll("_", " ")} completed`,
        tone: "success",
      });
    });

    if (closeRun.approvedAt !== null) {
      items.push({
        badge: "Sign-off",
        detail: "The close run reached approved state and is ready for release controls.",
        id: `${closeRun.id}-approved`,
        occurredAt: closeRun.approvedAt,
        title: "Close run approved",
        tone: "success",
      });
    }

    if (closeRun.archivedAt !== null) {
      items.push({
        badge: "Archive",
        detail: "The close run was archived after release or review completion.",
        id: `${closeRun.id}-archived`,
        occurredAt: closeRun.archivedAt,
        title: "Close run archived",
        tone: "warning",
      });
    }

    return items
      .sort(
        (left, right) => new Date(right.occurredAt).valueOf() - new Date(left.occurredAt).valueOf(),
      )
      .map((item) => ({
        badge: item.badge,
        detail: item.detail,
        id: item.id,
        timestamp: formatCloseRunDateTime(item.occurredAt),
        title: item.title,
        tone: item.tone,
      }));
  }, [workspaceData]);

  if (isLoading) {
    return (
      <div className="app-shell close-run-overview-page">
        <SurfaceCard title="Loading Close Run" subtitle="Overview">
          <p className="form-helper">
            Loading close-run status, phase progress, and workspace context...
          </p>
        </SurfaceCard>
      </div>
    );
  }

  if (workspaceData === null) {
    return (
      <div className="app-shell close-run-overview-page">
        <SurfaceCard title="Close Run Unavailable" subtitle="Overview">
          <div className="status-banner danger" role="alert">
            {errorMessage ?? "The requested close run could not be loaded."}
          </div>
        </SurfaceCard>
      </div>
    );
  }

  const closeRun = workspaceData.closeRun;
  const activePhase = findActivePhase(closeRun);
  const attention = deriveCloseRunAttention(closeRun);

  return (
    <div className="app-shell close-run-overview-page">
      <section className="hero-grid close-run-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Close Run Overview</p>
          <h1>{formatCloseRunPeriod(closeRun)}</h1>
          <p className="lede">
            Use this overview to move between workflow phases, review queues, and related entity
            activity without losing the current period context.
          </p>

          <div className="close-run-action-row">
            <Link className="secondary-button" href={`/entities/${workspaceData.entity.id}`}>
              Back to entity workspace
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/documents`}
            >
              Document queue
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/reconciliation`}
            >
              Reconciliation
            </Link>
            <Link
              className="secondary-button"
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/chat`}
            >
              Copilot
            </Link>
          </div>
        </div>

        <SurfaceCard title="Close-run Snapshot" subtitle={workspaceData.entity.name} tone="accent">
          <dl className="entity-meta-grid close-run-summary-grid">
            <div>
              <dt>Status</dt>
              <dd>{getCloseRunStatusLabel(closeRun.status)}</dd>
            </div>
            <div>
              <dt>Reporting currency</dt>
              <dd>{closeRun.reportingCurrency}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>v{closeRun.currentVersionNo}</dd>
            </div>
            <div>
              <dt>Active phase</dt>
              <dd>{activePhase ? activePhase.phase.replaceAll("_", " ") : "No active phase"}</dd>
            </div>
          </dl>
          <p className="form-helper">{attention.detail}</p>
        </SurfaceCard>
      </section>

      {errorMessage ? (
        <div className="status-banner warning" role="status">
          {errorMessage}
        </div>
      ) : null}

      <section className="close-run-stat-grid">
        <MetricCard label="Opened" value={formatCloseRunDateTime(closeRun.createdAt)} />
        <MetricCard label="Last updated" value={formatCloseRunDateTime(closeRun.updatedAt)} />
        <MetricCard label="Approved" value={formatCloseRunDateTime(closeRun.approvedAt)} />
        <MetricCard
          label="Source version"
          value={closeRun.reopenedFromCloseRunId ?? "Current root"}
        />
      </section>

      <section className="content-grid">
        <SurfaceCard title="Phase Progress" subtitle="Five-phase workflow">
          <PhaseProgress items={buildPhaseProgressItems(closeRun)} />
        </SurfaceCard>

        <SurfaceCard title="Review Surfaces" subtitle="Jump into the work">
          <div className="dashboard-row-list">
            <QuickLinkRow
              description="Resolve collection blockers, low-confidence extractions, and wrong-period documents."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/documents`}
              label="Open document queue"
            />
            <QuickLinkRow
              description="Disposition unmatched items, anomalies, and supporting schedule exceptions."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/reconciliation`}
              label="Open reconciliation review"
            />
            <QuickLinkRow
              description="Ask grounded questions about this period's source documents, rules, and outputs."
              href={`/entities/${workspaceData.entity.id}/close-runs/${closeRun.id}/chat`}
              label="Open copilot"
            />
            <QuickLinkRow
              description="Inspect entity-wide report templates that control downstream report runs."
              href={`/entities/${workspaceData.entity.id}/reports/templates`}
              label="Open report templates"
            />
          </div>
        </SurfaceCard>
      </section>

      <section className="content-grid">
        <SurfaceCard title="Lifecycle Timeline" subtitle="Current period history">
          <Timeline
            emptyMessage="Lifecycle events will appear once this close run records phase completions or sign-off."
            items={timelineItems}
          />
        </SurfaceCard>

        <SurfaceCard title="Related Periods" subtitle="Other close runs in this entity">
          <div className="dashboard-row-list">
            {relatedCloseRuns.length === 0 ? (
              <p className="form-helper">No other close runs exist for this entity yet.</p>
            ) : (
              relatedCloseRuns.map((relatedRun) => (
                <article className="dashboard-row" key={relatedRun.id}>
                  <div className="close-run-row-header">
                    <div>
                      <strong className="close-run-row-title">
                        {formatCloseRunPeriod(relatedRun)}
                      </strong>
                      <p className="close-run-row-meta">
                        {getCloseRunStatusLabel(relatedRun.status)} • v{relatedRun.currentVersionNo}
                      </p>
                    </div>
                    <span className="entity-status-chip">
                      {deriveCloseRunAttention(relatedRun).label}
                    </span>
                  </div>
                  <div className="close-run-link-row">
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${workspaceData.entity.id}/close-runs/${relatedRun.id}`}
                    >
                      Open overview
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${workspaceData.entity.id}/close-runs/${relatedRun.id}/documents`}
                    >
                      Documents
                    </Link>
                  </div>
                </article>
              ))
            )}
          </div>
        </SurfaceCard>
      </section>
    </div>
  );
}

async function loadCloseRunWorkspace(options: {
  closeRunId: string;
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (value: CloseRunWorkspaceData) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const workspace = await readCloseRunWorkspace(options.entityId, options.closeRunId);
    options.onLoaded(workspace);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveCloseRunOverviewErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveCloseRunOverviewErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError || error instanceof CloseRunApiError) {
    return error.message;
  }

  return "The close-run overview could not be loaded. Reload the workspace and try again.";
}

function MetricCard({
  label,
  value,
}: Readonly<{
  label: string;
  value: string;
}>): ReactElement {
  return (
    <article className="dashboard-stat-block">
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  );
}

function QuickLinkRow({
  description,
  href,
  label,
}: Readonly<{
  description: string;
  href: string;
  label: string;
}>): ReactElement {
  return (
    <article className="dashboard-row">
      <strong className="close-run-row-title">{label}</strong>
      <p className="form-helper">{description}</p>
      <div className="close-run-link-row">
        <Link className="workspace-link-inline" href={href}>
          Open
        </Link>
      </div>
    </article>
  );
}
