"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../components/layout/QuartzIcons";
import {
  deriveCloseRunAttention,
  formatCloseRunPeriod,
  type CloseRunSummary,
} from "../../../lib/close-runs";
import {
  readDashboardBootstrap,
  readDashboardBootstrapSnapshot,
  type DashboardEntityRuns,
} from "../../../lib/dashboard";
import {
  EntityApiError,
  listEntities,
  readEntityListSnapshot,
  type EntitySummary,
} from "../../../lib/entities/api";
import {
  deriveRememberedCloseContextFromDashboardEntries,
  readRememberedCloseContext,
  subscribeRememberedCloseContext,
  writeRememberedCloseContext,
  type RememberedCloseContext,
} from "../../../lib/workspace-navigation";

type AssistantHubRow = Readonly<{
  entity: EntitySummary;
  latestCloseRun: CloseRunSummary | null;
}>;

export default function AssistantHubPage(): ReactElement {
  const dashboardSnapshot = readDashboardBootstrapSnapshot();
  const entityListSnapshot = readEntityListSnapshot();
  const [dashboardData, setDashboardData] = useState<readonly DashboardEntityRuns[]>(
    () => dashboardSnapshot ?? [],
  );
  const [entities, setEntities] = useState<readonly EntitySummary[]>(
    () => entityListSnapshot?.entities ?? [],
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(
    () => dashboardSnapshot === null && entityListSnapshot === null,
  );
  const [rememberedCloseContext, setRememberedCloseContext] =
    useState<RememberedCloseContext | null>(() => readRememberedCloseContext());

  useEffect(() => {
    return subscribeRememberedCloseContext((context) => {
      setRememberedCloseContext(context);
    });
  }, []);

  useEffect(() => {
    void loadAssistantHubData({
      onDashboardLoaded: setDashboardData,
      onEntitiesLoaded: setEntities,
      onError: setErrorMessage,
      onLoadingChange: setIsLoading,
      showLoading: dashboardSnapshot === null && entityListSnapshot === null,
    });
  }, [dashboardSnapshot, entityListSnapshot]);

  const rows = useMemo<readonly AssistantHubRow[]>(() => {
    const latestCloseByEntity = new Map<string, CloseRunSummary | null>(
      dashboardData.map((entry) => [entry.entity.id, entry.closeRuns[0] ?? null]),
    );

    return [...entities]
      .map((entity) => ({
        entity,
        latestCloseRun: latestCloseByEntity.get(entity.id) ?? null,
      }))
      .sort(compareAssistantHubRows);
  }, [dashboardData, entities]);

  const preferredCloseContext = useMemo(
    () => rememberedCloseContext ?? deriveRememberedCloseContextFromDashboardEntries(dashboardData),
    [dashboardData, rememberedCloseContext],
  );

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Assistant Hub</h1>
            <p className="quartz-page-subtitle">
              Choose the right scope before you work. Entity assistant handles workspace control.
              Close assistant handles one exact reporting run.
            </p>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Entity Assistant</p>
                  <h3>Workspace-wide control</h3>
                </div>
              </div>
              <p className="quartz-page-subtitle">
                Use this when you want to manage workspace data, review available close runs, start
                a new close, or upload COA, general ledger, and trial balance files.
              </p>
            </article>

            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Close Assistant</p>
                  <h3>One period, one governed run</h3>
                </div>
              </div>
              <p className="quartz-page-subtitle">
                Use this when you want the assistant grounded to one exact close run for document
                review, recommendations, reconciliation, reporting, and release work.
              </p>
            </article>
          </div>
        </section>

        {preferredCloseContext ? (
          <section className="quartz-section">
            <article className="quartz-card">
              <div className="quartz-section-header">
                <div>
                  <p className="quartz-card-eyebrow">Resume</p>
                  <h2 className="quartz-section-title">Latest close assistant</h2>
                </div>
                <Link className="quartz-filter-link" href={preferredCloseContext.chatHref}>
                  <QuartzIcon className="quartz-inline-icon" name="assistant" />
                  Open latest close
                </Link>
              </div>
              <p className="quartz-page-subtitle">
                Jump back into the most recent close-run assistant without making the sidebar guess
                your scope for you.
              </p>
            </article>
          </section>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Assistant Entry Points</h2>
            <span className="quartz-queue-meta">
              {rows.length} workspace{rows.length === 1 ? "" : "s"}
            </span>
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Workspace</th>
                  <th>Latest Close</th>
                  <th>Scope Guidance</th>
                  <th>Launch</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="quartz-empty-state">Loading assistant scopes...</div>
                    </td>
                  </tr>
                ) : rows.length === 0 ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="quartz-empty-state">
                        No workspaces exist yet. Create the first workspace before opening the
                        assistant.
                      </div>
                    </td>
                  </tr>
                ) : (
                  rows.map((row) => (
                    <tr key={row.entity.id}>
                      <td>
                        <div className="quartz-table-primary">{row.entity.name}</div>
                        <div className="quartz-table-secondary">
                          {row.entity.base_currency} workspace •{" "}
                          {row.entity.last_activity?.summary ?? "No governed activity yet"}
                        </div>
                      </td>
                      <td>
                        {row.latestCloseRun ? (
                          <>
                            <div className="quartz-table-primary">
                              {formatCloseRunPeriod(row.latestCloseRun)}
                            </div>
                            <div className="quartz-table-secondary">
                              {deriveCloseRunAttention(row.latestCloseRun).detail}
                            </div>
                          </>
                        ) : (
                          <>
                            <div className="quartz-table-primary">No close run yet</div>
                            <div className="quartz-table-secondary">
                              Start at entity scope, then choose or create a run.
                            </div>
                          </>
                        )}
                      </td>
                      <td>
                        <div className="quartz-table-primary">
                          {row.latestCloseRun
                            ? "Use entity scope to choose or use close scope to execute directly."
                            : "Use entity scope for workspace setup and close-run creation."}
                        </div>
                      </td>
                      <td className="quartz-table-center">
                        <div className="quartz-inline-actions">
                          <Link
                            className="quartz-action-link"
                            href={`/entities/${row.entity.id}/assistant`}
                          >
                            Entity
                          </Link>
                          {row.latestCloseRun ? (
                            <Link
                              className="quartz-action-link"
                              href={`/entities/${row.entity.id}/close-runs/${row.latestCloseRun.id}/chat`}
                            >
                              Close
                            </Link>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      </section>
    </div>
  );
}

async function loadAssistantHubData(options: {
  onDashboardLoaded: (entries: readonly DashboardEntityRuns[]) => void;
  onEntitiesLoaded: (entities: readonly EntitySummary[]) => void;
  onError: (message: string | null) => void;
  onLoadingChange: (value: boolean) => void;
  showLoading: boolean;
}): Promise<void> {
  if (options.showLoading) {
    options.onLoadingChange(true);
  }

  const [dashboardResult, entityResult] = await Promise.allSettled([
    readDashboardBootstrap(),
    listEntities(),
  ]);

  if (dashboardResult.status === "fulfilled") {
    options.onDashboardLoaded(dashboardResult.value);
    const preferredCloseContext = deriveRememberedCloseContextFromDashboardEntries(
      dashboardResult.value,
    );
    if (preferredCloseContext !== null) {
      writeRememberedCloseContext(preferredCloseContext);
    }
  }

  if (entityResult.status === "fulfilled") {
    options.onEntitiesLoaded(entityResult.value.entities);
  }

  if (dashboardResult.status === "rejected" && entityResult.status === "rejected") {
    options.onError(resolveAssistantHubErrorMessage(dashboardResult.reason));
  } else if (dashboardResult.status === "rejected") {
    options.onError(resolveAssistantHubErrorMessage(dashboardResult.reason));
  } else if (entityResult.status === "rejected") {
    options.onError(resolveAssistantHubErrorMessage(entityResult.reason));
  } else {
    options.onError(null);
  }

  if (options.showLoading) {
    options.onLoadingChange(false);
  }
}

function resolveAssistantHubErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError) {
    return error.message;
  }
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }
  return "The assistant hub could not be loaded. Reload the workspace and try again.";
}

function compareAssistantHubRows(left: AssistantHubRow, right: AssistantHubRow): number {
  const rightTimestamp = right.entity.last_activity?.created_at;
  const leftTimestamp = left.entity.last_activity?.created_at;
  if (leftTimestamp && rightTimestamp) {
    const delta = new Date(rightTimestamp).valueOf() - new Date(leftTimestamp).valueOf();
    if (delta !== 0) {
      return delta;
    }
  }
  if (rightTimestamp) {
    return 1;
  }
  if (leftTimestamp) {
    return -1;
  }
  return left.entity.name.localeCompare(right.entity.name);
}
