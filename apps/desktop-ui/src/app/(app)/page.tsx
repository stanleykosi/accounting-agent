/*
Purpose: Render the global desktop dashboard for close-run operations, review queues, and recent workspace activity.
Scope: Authenticated dashboard data loading, status aggregation, featured close-run progress, and review-queue navigation.
Dependencies: Same-origin entity and close-run API helpers plus shared desktop UI components.
*/

"use client";

import { PhaseProgress, SurfaceCard, Timeline, type TimelineItem } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { useEffect, useMemo, useState, type ReactElement } from "react";
import {
  CloseRunApiError,
  buildPhaseProgressItems,
  deriveCloseRunAttention,
  findBlockingPhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  listCloseRuns,
  type CloseRunSummary,
} from "../../lib/close-runs";
import { EntityApiError, listEntities, type EntitySummary } from "../../lib/entities/api";

type DashboardEntityRuns = {
  entity: EntitySummary;
  closeRuns: readonly CloseRunSummary[];
};

type DashboardRow = {
  closeRun: CloseRunSummary;
  entity: EntitySummary;
};

type DashboardActivityRow = {
  entityName: string;
  lastActivity: NonNullable<EntitySummary["last_activity"]>;
};

/**
 * Purpose: Render the primary authenticated dashboard for desktop operators.
 * Inputs: None.
 * Outputs: A client-rendered dashboard with high-signal close-run navigation and activity context.
 * Behavior: Loads accessible entities first, then hydrates each entity's close runs in parallel through the same-origin proxy.
 */
export default function DashboardPage(): ReactElement {
  const [dashboardData, setDashboardData] = useState<readonly DashboardEntityRuns[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    void loadDashboard({
      onError: setErrorMessage,
      onLoaded: setDashboardData,
      onLoadingChange: setIsLoading,
    });
  }, []);

  const flattenedRows = useMemo<readonly DashboardRow[]>(
    () =>
      dashboardData.flatMap((entry) =>
        entry.closeRuns.map((closeRun) => ({
          closeRun,
          entity: entry.entity,
        })),
      ),
    [dashboardData],
  );

  const reviewQueue = useMemo(() => [...flattenedRows].sort(compareDashboardRows), [flattenedRows]);

  const featuredRow = reviewQueue[0] ?? null;
  const recentActivityItems = useMemo<readonly TimelineItem[]>(() => {
    const activityRows: DashboardActivityRow[] = [];
    dashboardData.forEach((entry) => {
      const lastActivity = entry.entity.last_activity;
      if (lastActivity === null || lastActivity === undefined) {
        return;
      }

      activityRows.push({
        entityName: entry.entity.name,
        lastActivity,
      });
    });

    return activityRows
      .sort((left, right) => {
        const leftTimestamp = new Date(left.lastActivity.created_at).valueOf();
        const rightTimestamp = new Date(right.lastActivity.created_at).valueOf();
        return rightTimestamp - leftTimestamp;
      })
      .slice(0, 6)
      .map(({ entityName, lastActivity }) => {
        return {
          badge: entityName,
          detail: `${lastActivity.summary} via ${lastActivity.source_surface}.`,
          id: lastActivity.id,
          timestamp: formatCloseRunDateTime(lastActivity.created_at),
          title: lastActivity.summary,
          tone: "default",
        } satisfies TimelineItem;
      });
  }, [dashboardData]);

  const openCloseRuns = flattenedRows.filter(({ closeRun }) =>
    ["draft", "in_review", "reopened"].includes(closeRun.status),
  ).length;
  const blockedPhaseCount = flattenedRows.reduce(
    (count, row) =>
      count +
      row.closeRun.workflowState.phaseStates.filter((phase) => phase.status === "blocked").length,
    0,
  );
  const readyForSignoffCount = flattenedRows.filter(({ closeRun }) =>
    closeRun.workflowState.phaseStates.some(
      (phaseState) => phaseState.phase === "review_signoff" && phaseState.status === "ready",
    ),
  ).length;
  const showEmptyDashboardState = !isLoading && errorMessage === null && dashboardData.length === 0;
  const showEntityWorkspacesSection = !isLoading && dashboardData.length > 0;
  const showReviewQueueSection = !isLoading && flattenedRows.length > 0;

  return (
    <div className="app-shell dashboard-page">
      <section className="hero-grid dashboard-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Global Dashboard</p>
          <h1>
            Track period progress, review pressure, and entity activity from one desktop home.
          </h1>
          <p className="lede">
            Keep the five-phase workflow visible across every entity so accountants can move from
            blockers into sign-off without losing the audit trail.
          </p>
          <div className="close-run-action-row">
            <Link className="primary-button" href="/entities">
              Open entity workspaces
            </Link>
          </div>
        </div>

        <SurfaceCard title="Today's Focus" subtitle="Workspace summary" tone="accent">
          <div className="dashboard-stat-grid">
            <StatBlock label="Entities" value={String(dashboardData.length)} />
            <StatBlock label="Open close runs" value={String(openCloseRuns)} />
            <StatBlock label="Blocked phases" value={String(blockedPhaseCount)} />
            <StatBlock label="Ready for sign-off" value={String(readyForSignoffCount)} />
          </div>
        </SurfaceCard>
      </section>

      {errorMessage ? (
        <div className="status-banner warning" role="status">
          {errorMessage}
        </div>
      ) : null}

      {isLoading ? (
        <SurfaceCard title="Loading Dashboard" subtitle="Global workspace">
          <p className="form-helper">
            Loading entity workspaces, close-run status, and review signals...
          </p>
        </SurfaceCard>
      ) : null}

      {showEmptyDashboardState ? (
        <SurfaceCard title="No Entity Workspaces Yet" subtitle="Global dashboard">
          <p className="form-helper">
            Create the first entity workspace to start tracking period close runs and workflow
            progress.
          </p>
        </SurfaceCard>
      ) : null}

      {!isLoading && featuredRow !== null ? (
        <section className="content-grid">
          <SurfaceCard title="Featured Close Run" subtitle={featuredRow.entity.name}>
            <div className="close-run-row-header">
              <div>
                <strong className="close-run-row-title">
                  {formatCloseRunPeriod(featuredRow.closeRun)}
                </strong>
                <p className="close-run-row-meta">
                  {getCloseRunStatusLabel(featuredRow.closeRun.status)} • Updated{" "}
                  {formatCloseRunDateTime(featuredRow.closeRun.updatedAt)}
                </p>
              </div>
              <span
                className={`timeline-badge ${deriveCloseRunAttention(featuredRow.closeRun).tone}`}
              >
                {deriveCloseRunAttention(featuredRow.closeRun).label}
              </span>
            </div>

            <p className="form-helper">{deriveCloseRunAttention(featuredRow.closeRun).detail}</p>
            <PhaseProgress items={buildPhaseProgressItems(featuredRow.closeRun)} />

            <div className="close-run-link-row">
              <Link
                className="secondary-button"
                href={`/entities/${featuredRow.entity.id}/close-runs/${featuredRow.closeRun.id}`}
              >
                Open overview
              </Link>
              <Link
                className="secondary-button"
                href={`/entities/${featuredRow.entity.id}/close-runs/${featuredRow.closeRun.id}/documents`}
              >
                Review documents
              </Link>
            </div>
          </SurfaceCard>

          <SurfaceCard title="Recent Activity" subtitle="Workspace timeline">
            <Timeline
              emptyMessage="Activity appears here once entity workspaces begin recording events."
              items={recentActivityItems}
            />
          </SurfaceCard>
        </section>
      ) : null}

      {showReviewQueueSection ? (
        <section className="content-grid">
          <SurfaceCard title="Review Queue" subtitle="Highest-attention close runs">
            <div className="dashboard-row-list">
              {reviewQueue.slice(0, 6).map((row) => (
                <article className="dashboard-row" key={row.closeRun.id}>
                  <div className="close-run-row-header">
                    <div>
                      <strong className="close-run-row-title">{row.entity.name}</strong>
                      <p className="close-run-row-meta">{formatCloseRunPeriod(row.closeRun)}</p>
                    </div>
                    <span className="entity-status-chip">
                      {getCloseRunStatusLabel(row.closeRun.status)}
                    </span>
                  </div>

                  <p className="form-helper">{deriveCloseRunAttention(row.closeRun).detail}</p>

                  <div className="close-run-link-row">
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${row.entity.id}/close-runs/${row.closeRun.id}`}
                    >
                      Overview
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${row.entity.id}/close-runs/${row.closeRun.id}/documents`}
                    >
                      Documents
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${row.entity.id}/close-runs/${row.closeRun.id}/reconciliation`}
                    >
                      Reconciliation
                    </Link>
                  </div>
                </article>
              ))}
            </div>
          </SurfaceCard>
        </section>
      ) : null}

      {showEntityWorkspacesSection ? (
        <section className="content-grid">
          <SurfaceCard title="Entity Workspaces" subtitle="Latest workspace context">
            <div className="dashboard-row-list">
              {dashboardData.map((entry) => (
                <article className="dashboard-row" key={entry.entity.id}>
                  <div className="close-run-row-header">
                    <div>
                      <strong className="close-run-row-title">{entry.entity.name}</strong>
                      <p className="close-run-row-meta">
                        {entry.entity.base_currency} • {entry.entity.member_count} members
                      </p>
                    </div>
                    <span className="entity-status-chip">
                      {entry.entity.autonomy_mode.replaceAll("_", " ")}
                    </span>
                  </div>

                  <p className="form-helper">
                    {entry.closeRuns[0]
                      ? `${entry.closeRuns.length} close runs recorded. Latest period: ${formatCloseRunPeriod(entry.closeRuns[0])}.`
                      : "No close runs recorded yet for this workspace."}
                  </p>

                  <div className="close-run-link-row">
                    <Link className="workspace-link-inline" href={`/entities/${entry.entity.id}`}>
                      Open workspace
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entry.entity.id}/coa`}
                    >
                      Chart of accounts
                    </Link>
                    <Link
                      className="workspace-link-inline"
                      href={`/entities/${entry.entity.id}/integrations`}
                    >
                      Integrations
                    </Link>
                  </div>
                </article>
              ))}
            </div>
          </SurfaceCard>
        </section>
      ) : null}
    </div>
  );
}

async function loadDashboard(options: {
  onError: (message: string | null) => void;
  onLoaded: (value: readonly DashboardEntityRuns[]) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const entityList = await listEntities();
    const closeRunsByEntity = await Promise.all(
      entityList.entities.map(async (entity) => ({
        closeRuns: await listCloseRuns(entity.id),
        entity,
      })),
    );
    options.onLoaded(closeRunsByEntity);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveDashboardErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveDashboardErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError || error instanceof CloseRunApiError) {
    return error.message;
  }

  return "The dashboard could not be loaded. Reload the workspace and try again.";
}

function compareDashboardRows(left: DashboardRow, right: DashboardRow): number {
  return (
    dashboardPriority(left.closeRun) - dashboardPriority(right.closeRun) ||
    compareUpdatedAt(left.closeRun, right.closeRun)
  );
}

function dashboardPriority(closeRun: CloseRunSummary): number {
  if (findBlockingPhase(closeRun) !== null) {
    return 0;
  }

  if (
    closeRun.workflowState.phaseStates.some(
      (phaseState) => phaseState.phase === "review_signoff" && phaseState.status === "ready",
    )
  ) {
    return 1;
  }

  if (closeRun.status === "in_review" || closeRun.status === "reopened") {
    return 2;
  }

  if (closeRun.status === "draft") {
    return 3;
  }

  return 4;
}

function compareUpdatedAt(left: CloseRunSummary, right: CloseRunSummary): number {
  return new Date(right.updatedAt).valueOf() - new Date(left.updatedAt).valueOf();
}

function StatBlock({
  label,
  value,
}: Readonly<{
  label: string;
  value: string;
}>): ReactElement {
  return (
    <div className="dashboard-stat-block">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
