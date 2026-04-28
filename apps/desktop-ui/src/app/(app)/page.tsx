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
  type CloseRunSummary,
} from "../../lib/close-runs";
import {
  readDashboardBootstrap,
  readDashboardBootstrapSnapshot,
  type DashboardEntityRuns,
} from "../../lib/dashboard";
import { EntityApiError, type EntitySummary } from "../../lib/entities/api";
import {
  deriveRememberedCloseContextFromDashboardEntries,
  writeRememberedCloseContext,
} from "../../lib/workspace-navigation";

type DashboardRow = {
  closeRun: CloseRunSummary;
  entity: EntitySummary;
};

type DashboardActivityRow = {
  entityName: string;
  lastActivity: NonNullable<EntitySummary["last_activity"]>;
};

type FocusedEntityRow = Readonly<{
  currentCloseRun: CloseRunSummary | null;
  entity: EntitySummary;
}>;

export default function DashboardPage(): ReactElement {
  const router = useRouter();
  const dashboardSnapshot = readDashboardBootstrapSnapshot();
  const [dashboardData, setDashboardData] = useState<readonly DashboardEntityRuns[]>(
    () => dashboardSnapshot ?? [],
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(() => dashboardSnapshot === null);

  useEffect(() => {
    void loadDashboard({
      onError: setErrorMessage,
      onLoaded: setDashboardData,
      onLoadingChange: setIsLoading,
      showLoading: dashboardSnapshot === null,
    });
  }, [dashboardSnapshot]);

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

  const focusedEntityEntry = useMemo<FocusedEntityRow | null>(() => {
    const entry = [...dashboardData].sort(compareEntityLedgerEntries)[0] ?? null;
    if (entry === null) {
      return null;
    }

    return {
      currentCloseRun: entry.closeRuns[0] ?? null,
      entity: entry.entity,
    };
  }, [dashboardData]);

  useEffect(() => {
    const preferredCloseContext = deriveRememberedCloseContextFromDashboardEntries(dashboardData);
    if (preferredCloseContext !== null) {
      writeRememberedCloseContext(preferredCloseContext);
    }
  }, [dashboardData]);

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
        .slice(0, 5),
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
  return (
    <div className="quartz-page quartz-workspace-layout quartz-portfolio-page">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Portfolio Overview</h1>
            <p className="quartz-page-subtitle">
              Current entity focus and the latest governed activity across active workspaces.
            </p>
          </div>
          <div>
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

        {!isLoading ? (
          <>
            <section className="quartz-section">
              <div className="quartz-kpi-grid quartz-portfolio-summary-grid">
                <article className="quartz-kpi-tile">
                  <p className="quartz-kpi-label">Total Entities</p>
                  <p className="quartz-kpi-value">{dashboardData.length}</p>
                  <p className="quartz-kpi-meta">Accessible workspaces</p>
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
                <h2 className="quartz-section-title">Current Entity</h2>
                <Link className="quartz-filter-link" href="/entities">
                  <QuartzIcon className="quartz-inline-icon" name="entities" />
                  Open Entity Directory
                </Link>
              </div>
              <div className="quartz-split-grid quartz-split-grid-halves">
                <article className="quartz-card">
                  {focusedEntityEntry === null ? (
                    <p className="form-helper">
                      The most recent entity workspace will appear here once work begins.
                    </p>
                  ) : (
                    <div className="quartz-table-shell">
                      <table className="quartz-table">
                        <thead>
                          <tr>
                            <th>Entity</th>
                            <th>Status</th>
                            <th>Latest Period</th>
                            <th>Action</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr
                            className={
                              resolveEntityAttention(focusedEntityEntry.currentCloseRun)?.tone ===
                              "warning"
                                ? "quartz-table-row error"
                                : undefined
                            }
                          >
                            <td>
                              <div className="quartz-table-primary">
                                {focusedEntityEntry.entity.name}
                              </div>
                              <div className="quartz-table-secondary">
                                {focusedEntityEntry.entity.base_currency} workspace •{" "}
                                {focusedEntityEntry.entity.member_count} members
                              </div>
                            </td>
                            <td>
                              <span
                                className={`quartz-status-badge ${resolveEntityBadgeTone(focusedEntityEntry)}`}
                              >
                                {resolveEntityAttention(focusedEntityEntry.currentCloseRun)
                                  ?.label ?? focusedEntityEntry.entity.status}
                              </span>
                            </td>
                            <td>
                              {focusedEntityEntry.currentCloseRun
                                ? formatCloseRunPeriod(focusedEntityEntry.currentCloseRun)
                                : "No close run"}
                            </td>
                            <td className="quartz-table-center">
                              <Link
                                className="quartz-action-link"
                                href={
                                  focusedEntityEntry.currentCloseRun
                                    ? `/entities/${focusedEntityEntry.entity.id}/close-runs/${focusedEntityEntry.currentCloseRun.id}`
                                    : `/entities/${focusedEntityEntry.entity.id}`
                                }
                              >
                                {focusedEntityEntry.currentCloseRun ? "Review" : "Open"}
                              </Link>
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  )}
                </article>

                <article className="quartz-card">
                  <div className="quartz-section-header quartz-section-header-tight">
                    <h2 className="quartz-section-title">Recent Activity</h2>
                  </div>
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
            </section>
          </>
        ) : null}
      </section>
    </div>
  );
}

async function loadDashboard(options: {
  onError: (message: string | null) => void;
  onLoaded: (value: readonly DashboardEntityRuns[]) => void;
  onLoadingChange: (value: boolean) => void;
  showLoading: boolean;
}): Promise<void> {
  if (options.showLoading) {
    options.onLoadingChange(true);
  }
  try {
    options.onLoaded(await readDashboardBootstrap());
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveDashboardErrorMessage(error));
  } finally {
    if (options.showLoading) {
      options.onLoadingChange(false);
    }
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

function compareEntityLedgerEntries(left: DashboardEntityRuns, right: DashboardEntityRuns): number {
  const leftTimestamp = resolveEntityRecencyTimestamp(left);
  const rightTimestamp = resolveEntityRecencyTimestamp(right);

  if (leftTimestamp !== rightTimestamp) {
    return rightTimestamp - leftTimestamp;
  }

  const leftRun = left.closeRuns[0] ?? null;
  const rightRun = right.closeRuns[0] ?? null;
  if (leftRun !== null && rightRun !== null) {
    return compareDashboardRows(
      { closeRun: leftRun, entity: left.entity },
      { closeRun: rightRun, entity: right.entity },
    );
  }

  if (leftRun !== null) {
    return -1;
  }

  if (rightRun !== null) {
    return 1;
  }

  return left.entity.name.localeCompare(right.entity.name);
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

function resolveEntityRecencyTimestamp(entry: DashboardEntityRuns): number {
  const activityTimestamp = entry.entity.last_activity?.created_at;
  if (typeof activityTimestamp === "string") {
    return new Date(activityTimestamp).valueOf();
  }

  const closeRunTimestamp = entry.closeRuns[0]?.updatedAt;
  if (typeof closeRunTimestamp === "string") {
    return new Date(closeRunTimestamp).valueOf();
  }

  return Number.NEGATIVE_INFINITY;
}

function resolveEntityAttention(closeRun: CloseRunSummary | null) {
  return closeRun === null ? null : deriveCloseRunAttention(closeRun);
}

function resolveEntityBadgeTone(
  entry: Readonly<{
    currentCloseRun?: CloseRunSummary | null;
    entity: EntitySummary;
  }>,
): "error" | "neutral" | "success" {
  const attention = resolveEntityAttention(entry.currentCloseRun ?? null);
  if (attention?.tone === "warning") {
    return "error";
  }

  return entry.entity.status === "archived" ? "neutral" : "success";
}
