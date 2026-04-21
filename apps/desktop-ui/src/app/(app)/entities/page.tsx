/*
Purpose: Render the entity directory with a clean operational index into workspaces.
Scope: Client-side entity listing through the same-origin proxy, plus quick routes into workspace creation and entity homes.
Dependencies: React hooks, Next.js links, and the entity API helpers.
*/

"use client";

import Link from "next/link";
import { useEffect, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../components/layout/QuartzIcons";
import { readDashboardBootstrap, readDashboardBootstrapSnapshot } from "../../../lib/dashboard";
import {
  EntityApiError,
  listEntities,
  readEntityListSnapshot,
  type EntitySummary,
} from "../../../lib/entities/api";
import {
  deriveRememberedCloseContextFromDashboardEntries,
  writeRememberedCloseContext,
} from "../../../lib/workspace-navigation";

export default function EntitiesPage(): ReactElement {
  const entityListSnapshot = readEntityListSnapshot();
  const [entities, setEntities] = useState<readonly EntitySummary[]>(
    () => entityListSnapshot?.entities ?? [],
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(() => entityListSnapshot === null);

  useEffect(() => {
    void loadEntities({
      onError: setErrorMessage,
      onLoaded: setEntities,
      onLoadingChange: setIsLoading,
      showLoading: entityListSnapshot === null,
    });
  }, [entityListSnapshot]);

  useEffect(() => {
    if (readDashboardBootstrapSnapshot() !== null) {
      return;
    }

    void readDashboardBootstrap()
      .then((entries) => {
        const preferredCloseContext = deriveRememberedCloseContextFromDashboardEntries(entries);
        if (preferredCloseContext !== null) {
          writeRememberedCloseContext(preferredCloseContext);
        }
      })
      .catch(() => {
        // Keep entity directory responsive even when dashboard prewarm fails.
      });
  }, []);

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Entity Directory</h1>
            <p className="quartz-page-subtitle">
              Portfolio-wide access to operational workspaces, ownership, and base-ledger defaults.
            </p>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {errorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid">
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Accessible Workspaces</p>
              <p className="quartz-kpi-value">{entities.length}</p>
              <p className="quartz-kpi-meta">Entities available to the current operator</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Default Currency</p>
              <p className="quartz-kpi-value">{entities[0]?.base_currency ?? "NGN"}</p>
              <p className="quartz-kpi-meta">Most recent workspace base ledger</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Assigned Operators</p>
              <p className="quartz-kpi-value">
                {entities.reduce((sum, entity) => sum + entity.member_count, 0)}
              </p>
              <p className="quartz-kpi-meta">Current memberships across listed entities</p>
            </article>
            <article className="quartz-kpi-tile highlight">
              <p className="quartz-kpi-label">Directory Status</p>
              <p className="quartz-kpi-value">{isLoading ? "Syncing" : "Current"}</p>
              <p className="quartz-kpi-meta">Fast local snapshot with current-state refresh</p>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Workspace Ledger</h2>
            <Link className="quartz-filter-link" href="/entities/new">
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              New workspace
            </Link>
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Entity</th>
                  <th>Base Ledger</th>
                  <th>Owner</th>
                  <th>Last Activity</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="quartz-empty-state">Loading entity directory...</div>
                    </td>
                  </tr>
                ) : entities.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="quartz-empty-state">
                        No workspaces exist yet. Create the first entity to begin the close
                        workflow.
                      </div>
                    </td>
                  </tr>
                ) : (
                  entities.map((entity) => (
                    <tr key={entity.id}>
                      <td>
                        <div className="quartz-table-primary">{entity.name}</div>
                        <div className="quartz-table-secondary">
                          {entity.legal_name ?? "Legal name not set"}
                        </div>
                      </td>
                      <td>
                        <div className="quartz-table-primary">{entity.base_currency}</div>
                        <div className="quartz-table-secondary">
                          {entity.country_code} • {entity.timezone}
                        </div>
                      </td>
                      <td>
                        <div className="quartz-table-primary">
                          {entity.default_actor?.full_name ?? "Default actor not assigned"}
                        </div>
                        <div className="quartz-table-secondary">
                          {entity.member_count} active members
                        </div>
                      </td>
                      <td>
                        <div className="quartz-table-primary">
                          {entity.last_activity?.summary ?? "No activity recorded"}
                        </div>
                        <div className="quartz-table-secondary">
                          {entity.last_activity
                            ? `${formatDateTime(entity.last_activity.created_at)} via ${entity.last_activity.source_surface}`
                            : "Awaiting first governed action"}
                        </div>
                      </td>
                      <td className="quartz-table-center">
                        <Link className="quartz-action-link" href={`/entities/${entity.id}`}>
                          Open
                        </Link>
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

async function loadEntities(options: {
  onError: (message: string | null) => void;
  onLoaded: (entities: readonly EntitySummary[]) => void;
  onLoadingChange: (value: boolean) => void;
  showLoading: boolean;
}): Promise<void> {
  if (options.showLoading) {
    options.onLoadingChange(true);
  }
  try {
    const response = await listEntities();
    options.onLoaded(response.entities);
    options.onError(null);
  } catch (error: unknown) {
    if (options.showLoading) {
      options.onLoaded([]);
    }
    options.onError(resolveEntityErrorMessage(error));
  } finally {
    if (options.showLoading) {
      options.onLoadingChange(false);
    }
  }
}

function resolveEntityErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError) {
    return error.message;
  }

  return "The entity directory could not be loaded. Reload the workspace and try again.";
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
