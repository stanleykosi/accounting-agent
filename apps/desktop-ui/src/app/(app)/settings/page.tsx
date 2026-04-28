"use client";

import Link from "next/link";
import { useEffect, useState, type ReactElement } from "react";
import { QuartzIcon } from "../../../components/layout/QuartzIcons";
import {
  AuthApiError,
  readCurrentSession,
  type AuthSessionResponse,
} from "../../../lib/auth/client";
import {
  EntityApiError,
  listEntities,
  readEntityListSnapshot,
  type EntitySummary,
} from "../../../lib/entities/api";

export default function GlobalSettingsPage(): ReactElement {
  const entityListSnapshot = readEntityListSnapshot();
  const [session, setSession] = useState<AuthSessionResponse | null>(null);
  const [entities, setEntities] = useState<readonly EntitySummary[]>(
    () => entityListSnapshot?.entities ?? [],
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(() => entityListSnapshot === null);

  useEffect(() => {
    void loadSettingsData({
      onEntitiesLoaded: setEntities,
      onError: setErrorMessage,
      onLoadingChange: setIsLoading,
      onSessionLoaded: setSession,
      showLoading: entityListSnapshot === null,
    });
  }, [entityListSnapshot]);

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Settings</h1>
            <p className="quartz-page-subtitle">
              Keep global settings lightweight. Product configuration lives at the workspace layer,
              while runtime checks stay under setup.
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
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Account</p>
                  <h2 className="quartz-section-title">Operator Profile</h2>
                </div>
              </div>

              {session ? (
                <dl className="quartz-settings-summary-grid">
                  <div className="quartz-settings-summary-row">
                    <dt>Full name</dt>
                    <dd>{session.user.full_name}</dd>
                  </div>
                  <div className="quartz-settings-summary-row">
                    <dt>Email</dt>
                    <dd>{session.user.email}</dd>
                  </div>
                  <div className="quartz-settings-summary-row">
                    <dt>Account status</dt>
                    <dd>{formatSentenceCase(session.user.status)}</dd>
                  </div>
                  <div className="quartz-settings-summary-row">
                    <dt>Last login</dt>
                    <dd>
                      {session.user.last_login_at
                        ? formatDateTime(session.user.last_login_at)
                        : "No login recorded"}
                    </dd>
                  </div>
                  <div className="quartz-settings-summary-row">
                    <dt>Session expiry</dt>
                    <dd>{formatDateTime(session.session.expires_at)}</dd>
                  </div>
                  <div className="quartz-settings-summary-row">
                    <dt>Session rotation</dt>
                    <dd>{session.session.rotated ? "Recently rotated" : "Stable"}</dd>
                  </div>
                </dl>
              ) : (
                <div className="quartz-empty-state quartz-empty-state-compact">
                  Loading account settings...
                </div>
              )}
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Environment</p>
                  <h2 className="quartz-section-title">Runtime Setup</h2>
                </div>
                <Link className="quartz-filter-link" href="/setup">
                  <QuartzIcon className="quartz-inline-icon" name="settings" />
                  Open Setup
                </Link>
              </div>
              <p className="quartz-page-subtitle">
                Setup is infrastructure readiness, not product configuration. Use it for runtime
                health, storage, worker, and dependency checks.
              </p>
              <div className="quartz-settings-info-stack">
                <div className="quartz-compact-pill">Product settings stay here</div>
                <div className="quartz-compact-pill">Infrastructure checks stay in setup</div>
              </div>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <div>
              <h2 className="quartz-section-title">Workspace Settings</h2>
              <p className="quartz-page-subtitle quartz-page-subtitle-tight">
                Accounting configuration is managed per entity workspace.
              </p>
            </div>
            <Link className="quartz-filter-link" href="/entities">
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Open Entity Directory
            </Link>
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Workspace</th>
                  <th>Base Ledger</th>
                  <th>Default Actor</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="quartz-empty-state quartz-empty-state-compact">
                        Loading workspace settings...
                      </div>
                    </td>
                  </tr>
                ) : entities.length === 0 ? (
                  <tr>
                    <td colSpan={4}>
                      <div className="quartz-empty-state">
                        No workspaces exist yet. Create a workspace first, then configure its
                        settings.
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
                          {formatAutonomyMode(entity.autonomy_mode)}
                        </div>
                      </td>
                      <td className="quartz-table-center">
                        <Link
                          className="quartz-action-link"
                          href={`/entities/${entity.id}/settings`}
                        >
                          Open workspace settings
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

async function loadSettingsData(options: {
  onEntitiesLoaded: (entities: readonly EntitySummary[]) => void;
  onError: (message: string | null) => void;
  onLoadingChange: (value: boolean) => void;
  onSessionLoaded: (session: AuthSessionResponse | null) => void;
  showLoading: boolean;
}): Promise<void> {
  if (options.showLoading) {
    options.onLoadingChange(true);
  }

  const [sessionResult, entitiesResult] = await Promise.allSettled([
    readCurrentSession(),
    listEntities(),
  ]);

  if (sessionResult.status === "fulfilled") {
    options.onSessionLoaded(sessionResult.value);
  }

  if (entitiesResult.status === "fulfilled") {
    options.onEntitiesLoaded(entitiesResult.value.entities);
  } else if (options.showLoading) {
    options.onEntitiesLoaded([]);
  }

  options.onError(resolveSettingsErrorMessage(sessionResult, entitiesResult));

  if (options.showLoading) {
    options.onLoadingChange(false);
  }
}

function resolveSettingsErrorMessage(
  sessionResult: PromiseSettledResult<AuthSessionResponse>,
  entitiesResult: PromiseSettledResult<{ entities: readonly EntitySummary[] }>,
): string | null {
  if (sessionResult.status === "rejected") {
    return resolveGlobalSettingsError(sessionResult.reason);
  }

  if (entitiesResult.status === "rejected") {
    return resolveGlobalSettingsError(entitiesResult.reason);
  }

  return null;
}

function resolveGlobalSettingsError(error: unknown): string {
  if (error instanceof AuthApiError || error instanceof EntityApiError) {
    return error.message;
  }

  return "The settings page could not be loaded. Reload the workspace and try again.";
}

function formatAutonomyMode(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatSentenceCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
