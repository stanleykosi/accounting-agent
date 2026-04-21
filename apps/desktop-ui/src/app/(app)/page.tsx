/*
Purpose: Render the global desktop dashboard for close-run operations, review queues, and recent workspace activity.
Scope: Authenticated dashboard data loading, status aggregation, featured close-run progress, and review-queue navigation.
Dependencies: Same-origin entity and close-run API helpers.
*/

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../components/layout/QuartzIcons";
import {
  CloseRunApiError,
  deriveCloseRunAttention,
  findBlockingPhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  listCloseRuns,
  type CloseRunSummary,
} from "../../lib/close-runs";
import { EntityApiError, listEntities, type EntitySummary } from "../../lib/entities/api";

type DashboardEntityRuns = {
  closeRuns: readonly CloseRunSummary[];
  entity: EntitySummary;
};

type DashboardRow = {
  closeRun: CloseRunSummary;
  entity: EntitySummary;
};

type DashboardActivityRow = {
  entityName: string;
  lastActivity: NonNullable<EntitySummary["last_activity"]>;
};

type DashboardEntitySummary = EntitySummary;

export default function DashboardPage(): ReactElement {
  const router = useRouter();
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

  useEffect(() => {
    if (!isLoading && errorMessage === null && dashboardData.length === 0) {
      router.replace("/entities/new");
    }
  }, [dashboardData.length, errorMessage, isLoading, router]);

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

  const entityRows = useMemo(
    () =>
      [...dashboardData].sort((left, right) => {
        const leftRun = left.closeRuns[0] ?? null;
        const rightRun = right.closeRuns[0] ?? null;

        if (leftRun === null && rightRun === null) {
          return left.entity.name.localeCompare(right.entity.name);
        }

        if (leftRun === null) {
          return 1;
        }

        if (rightRun === null) {
          return -1;
        }

        return compareDashboardRows(
          { closeRun: leftRun, entity: left.entity },
          { closeRun: rightRun, entity: right.entity },
        );
      }),
    [dashboardData],
  );

  const recentActivityRows = useMemo<readonly DashboardActivityRow[]>(
    () =>
      dashboardData
        .flatMap((entry) =>
          entry.entity.last_activity
            ? [
                {
                  entityName: entry.entity.name,
                  lastActivity: entry.entity.last_activity,
                },
              ]
            : [],
        )
        .sort(
          (left, right) =>
            new Date(right.lastActivity.created_at).valueOf() -
            new Date(left.lastActivity.created_at).valueOf(),
        )
        .slice(0, 4),
    [dashboardData],
  );

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
  const completionPercent =
    flattenedRows.length === 0
      ? 0
      : Math.round(
          (flattenedRows.reduce(
            (sum, row) =>
              sum +
              row.closeRun.workflowState.phaseStates.filter(
                (phaseState) => phaseState.status === "completed",
              ).length,
            0,
          ) /
            (flattenedRows.length * 5)) *
            100,
        );

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Portfolio Overview</h1>
            <p className="quartz-page-subtitle">
              Executive summary of active entities and close-run pressure across the current review
              cycle.
            </p>
          </div>
          <div>
            <p className="quartz-kpi-label">Portfolio Health</p>
            <p className="quartz-kpi-value">{openCloseRuns} Active Closes</p>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner warning" role="status" style={{ marginTop: "24px" }}>
            {errorMessage}
          </div>
        ) : null}

        {isLoading ? (
          <section className="quartz-section">
            <div className="quartz-empty-state">Loading the portfolio command center...</div>
          </section>
        ) : null}

        {!isLoading && featuredRow !== null ? (
          <>
            <section className="quartz-section">
              <div className="quartz-kpi-grid">
                <article className="quartz-kpi-tile">
                  <p className="quartz-kpi-label">Total Entities</p>
                  <p className="quartz-kpi-value">{dashboardData.length}</p>
                  <p className="quartz-kpi-meta">Accessible workspaces</p>
                </article>
                <article className="quartz-kpi-tile">
                  <p className="quartz-kpi-label">Close Progress</p>
                  <p className="quartz-kpi-value">{completionPercent}%</p>
                  <div className="quartz-progress-track">
                    <div
                      className="quartz-progress-bar"
                      style={{ width: `${completionPercent}%` }}
                    />
                  </div>
                </article>
                <article className="quartz-kpi-tile">
                  <p className="quartz-kpi-label">Active Exceptions</p>
                  <p className="quartz-kpi-value error">{blockedPhaseCount}</p>
                  <p className="quartz-kpi-meta">Blocked workflow phases</p>
                </article>
                <article className="quartz-kpi-tile highlight">
                  <p className="quartz-kpi-label">Ready For Sign-Off</p>
                  <p className="quartz-kpi-value">{readyForSignoffCount}</p>
                  <p className="quartz-kpi-meta">Periods awaiting final approval</p>
                </article>
              </div>
            </section>

            <section className="quartz-section">
              <div className="quartz-section-header">
                <h2 className="quartz-section-title">Entity Ledger</h2>
                <span className="quartz-filter-link">
                  <QuartzIcon className="quartz-inline-icon" name="filter" />
                  Prioritized view
                </span>
              </div>

              <div className="quartz-table-shell">
                <table className="quartz-table">
                  <thead>
                    <tr>
                      <th>Entity Name</th>
                      <th>Country</th>
                      <th>Status</th>
                      <th>Progress</th>
                      <th>Latest Period</th>
                      <th>Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entityRows.map((entry) => {
                      const currentCloseRun = entry.closeRuns[0] ?? null;
                      const attention = currentCloseRun
                        ? deriveCloseRunAttention(currentCloseRun)
                        : null;
                      const progress = currentCloseRun
                        ? calculateCompletionPercent(currentCloseRun)
                        : null;
                      const badgeTone =
                        attention?.tone === "warning"
                          ? "error"
                          : entry.entity.status === "archived"
                            ? "neutral"
                            : "success";

                      return (
                        <tr
                          className={
                            attention?.tone === "warning" ? "quartz-table-row error" : undefined
                          }
                          key={entry.entity.id}
                        >
                          <td>
                            <div className="quartz-table-primary">{entry.entity.name}</div>
                            <div className="quartz-table-secondary">
                              {entry.entity.base_currency} workspace • {entry.entity.member_count}{" "}
                              members
                            </div>
                          </td>
                          <td>{entry.entity.country_code}</td>
                          <td>
                            <span className={`quartz-status-badge ${badgeTone}`}>
                              {attention?.label ?? entry.entity.status}
                            </span>
                          </td>
                          <td className="quartz-table-numeric">
                            {progress === null ? "—" : `${progress}%`}
                          </td>
                          <td>
                            {currentCloseRun
                              ? formatCloseRunPeriod(currentCloseRun)
                              : "No close run"}
                          </td>
                          <td className="quartz-table-center">
                            <Link
                              className="quartz-action-link"
                              href={
                                currentCloseRun
                                  ? `/entities/${entry.entity.id}/close-runs/${currentCloseRun.id}`
                                  : `/entities/${entry.entity.id}`
                              }
                            >
                              {currentCloseRun ? "Review" : "Open"}
                            </Link>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        ) : null}
      </section>

      <aside className="quartz-right-rail">
        <div className="quartz-right-rail-header">
          <QuartzIcon className="quartz-inline-icon" name="assistant" />
          <div>
            <h2 className="quartz-right-rail-title">Omni-Assistant</h2>
            <p className="quartz-right-rail-subtitle">Portfolio intelligence</p>
          </div>
        </div>

        <div className="quartz-right-rail-body">
          {featuredRow ? (
            <article className="quartz-card ai">
              <p className="quartz-card-eyebrow error">Variance detected</p>
              <h3>{featuredRow.entity.name}</h3>
              <p className="form-helper">{deriveCloseRunAttention(featuredRow.closeRun).detail}</p>
              <div className="quartz-highlight-box">
                <span className="quartz-card-eyebrow">Current period</span>
                <p style={{ margin: "6px 0 0" }}>{formatCloseRunPeriod(featuredRow.closeRun)}</p>
              </div>
              <div className="quartz-button-row">
                <Link
                  className="secondary-button"
                  href={`/entities/${featuredRow.entity.id}/close-runs/${featuredRow.closeRun.id}`}
                >
                  Investigate
                </Link>
              </div>
            </article>
          ) : null}

          <article className="quartz-card">
            <p className="quartz-card-eyebrow secondary">Pace analysis</p>
            <h3>Close cadence is stable</h3>
            <p className="form-helper">
              {openCloseRuns} active close runs are in motion. {readyForSignoffCount} periods are
              ready for final review and {blockedPhaseCount} workflow gates currently require
              intervention.
            </p>
          </article>

          <article className="quartz-card">
            <p className="quartz-card-eyebrow">Recent activity</p>
            <div className="quartz-mini-list">
              {recentActivityRows.length === 0 ? (
                <p className="form-helper">Recent approvals and uploads will appear here.</p>
              ) : (
                recentActivityRows.map((activity) => (
                  <div className="quartz-mini-item" key={activity.lastActivity.id}>
                    <strong>{activity.lastActivity.summary}</strong>
                    <span className="quartz-mini-meta">
                      {activity.entityName} •{" "}
                      {formatCloseRunDateTime(activity.lastActivity.created_at)}
                    </span>
                  </div>
                ))
              )}
            </div>
          </article>
        </div>

        <div className="quartz-right-rail-footer">
          <Link className="primary-button" href="/entities">
            Open Entity Directory
          </Link>
        </div>
      </aside>
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
      entityList.entities.map(async (entity: DashboardEntitySummary) => ({
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

  return "The portfolio command center could not be loaded. Reload the workspace and try again.";
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

function calculateCompletionPercent(closeRun: Readonly<CloseRunSummary>): number {
  const completed = closeRun.workflowState.phaseStates.filter(
    (phaseState) => phaseState.status === "completed",
  ).length;
  return Math.round((completed / closeRun.workflowState.phaseStates.length) * 100);
}
