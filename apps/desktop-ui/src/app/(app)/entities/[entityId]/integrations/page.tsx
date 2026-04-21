/*
Purpose: Render entity integration management for QuickBooks Online inside the Quartz workspace shell.
Scope: Connection status, OAuth connect navigation, token revocation, and chart-of-accounts sync.
Dependencies: React client state, Next route params and links, and QuickBooks API helpers.
*/

"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState, useTransition, type ReactElement } from "react";
import { QuartzIcon } from "../../../../../components/layout/QuartzIcons";
import {
  QuickBooksApiError,
  disconnectQuickBooks,
  readQuickBooksStatus,
  startQuickBooksConnection,
  syncQuickBooksCoa,
  type QuickBooksCoaSyncResponse,
  type QuickBooksConnectionStatusResponse,
} from "../../../../../lib/quickbooks";
import { requireRouteParam } from "../../../../../lib/route-params";

/**
 * Purpose: Render the QuickBooks-first integration workspace for one entity.
 * Inputs: Route params containing the target entity UUID.
 * Outputs: A client-rendered integration management page with sync and reconnect controls.
 * Behavior: Keeps OAuth, token refresh, and COA materialization in backend services.
 */
export default function EntityIntegrationsPage(): ReactElement {
  const routeParams = useParams<{ entityId: string }>();
  const entityId = requireRouteParam(routeParams.entityId, "entityId");
  const [connectionStatus, setConnectionStatus] =
    useState<QuickBooksConnectionStatusResponse | null>(null);
  const [syncResult, setSyncResult] = useState<QuickBooksCoaSyncResponse | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void loadQuickBooksStatus({
      entityId,
      onError: setErrorMessage,
      onLoaded: setConnectionStatus,
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

  const handleConnect = (): void => {
    startTransition(() => {
      const returnUrl = `${window.location.origin}/entities/${entityId}/integrations`;
      void startQuickBooksConnection(entityId, returnUrl)
        .then((payload) => {
          window.location.assign(payload.authorization_url);
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveQuickBooksErrorMessage(error));
        });
    });
  };

  const handleDisconnect = (): void => {
    startTransition(() => {
      void disconnectQuickBooks(entityId)
        .then(() =>
          loadQuickBooksStatus({
            entityId,
            onError: setErrorMessage,
            onLoaded: setConnectionStatus,
            onLoadingChange: setIsLoading,
          }),
        )
        .then(() => {
          setSyncResult(null);
          setErrorMessage(null);
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveQuickBooksErrorMessage(error));
        });
    });
  };

  const handleSyncCoa = (): void => {
    startTransition(() => {
      void syncQuickBooksCoa(entityId)
        .then((result) => {
          setSyncResult(result);
          setErrorMessage(null);
          return loadQuickBooksStatus({
            entityId,
            onError: setErrorMessage,
            onLoaded: setConnectionStatus,
            onLoadingChange: setIsLoading,
          });
        })
        .catch((error: unknown) => {
          setErrorMessage(resolveQuickBooksErrorMessage(error));
        });
    });
  };

  if (isLoading && connectionStatus === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading integrations workspace...</div>
        </section>
      </div>
    );
  }

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>Integrations</h1>
            <p className="quartz-page-subtitle">
              Manage QuickBooks Online connectivity, token lifecycle, and chart-of-accounts sync
              from one governed workspace surface.
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <Link className="secondary-button quartz-toolbar-button" href={`/entities/${entityId}/settings`}>
              <QuartzIcon className="quartz-inline-icon" name="settings" />
              Workspace Settings
            </Link>
            <Link className="secondary-button quartz-toolbar-button" href={`/entities/${entityId}/coa`}>
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Chart of Accounts
            </Link>
          </div>
        </header>

        {errorMessage ? (
          <div className="status-banner danger quartz-section" role="alert">
            {errorMessage}
          </div>
        ) : null}

        {connectionStatus?.recovery_action ? (
          <div className="status-banner warning quartz-section" role="status">
            {connectionStatus.recovery_action}
          </div>
        ) : null}

        {syncResult ? (
          <div className="status-banner success quartz-section" role="status">
            {syncResult.message} COA version {syncResult.version_no}
            {syncResult.activated ? " is now active." : " was saved for activation when needed."}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid quartz-kpi-grid-triple">
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Connection Status</p>
              <p className="quartz-kpi-value">{formatStatus(connectionStatus?.status ?? "disconnected")}</p>
              <p className="quartz-kpi-meta">Current QuickBooks authorization posture</p>
            </article>
            <article className="quartz-kpi-tile">
              <p className="quartz-kpi-label">Realm</p>
              <p className="quartz-kpi-value quartz-kpi-value-small">
                {connectionStatus?.external_realm_id ?? "Not connected"}
              </p>
              <p className="quartz-kpi-meta">Connected company realm identifier</p>
            </article>
            <article className="quartz-kpi-tile highlight">
              <p className="quartz-kpi-label">Last Sync</p>
              <p className="quartz-kpi-value">
                {connectionStatus?.last_sync_at ? formatDateTime(connectionStatus.last_sync_at) : "No sync yet"}
              </p>
              <p className="quartz-kpi-meta">Latest COA import pulled from QuickBooks</p>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Connection</p>
                  <h2 className="quartz-section-title">OAuth Lifecycle</h2>
                </div>
              </div>
              <p className="quartz-page-subtitle">
                QuickBooks credentials are encrypted locally. Expired or revoked tokens require an
                explicit reconnect before another sync can run.
              </p>
              <div className="quartz-button-stack">
                <button className="primary-button" disabled={isPending} onClick={handleConnect} type="button">
                  {connectionStatus?.status === "connected" ? "Reconnect QuickBooks" : "Connect QuickBooks"}
                </button>
                <button
                  className="secondary-button"
                  disabled={isPending || connectionStatus?.status === "disconnected"}
                  onClick={handleDisconnect}
                  type="button"
                >
                  Disconnect QuickBooks
                </button>
              </div>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Synchronization</p>
                  <h2 className="quartz-section-title">Chart of Accounts Sync</h2>
                </div>
              </div>
              <p className="quartz-page-subtitle">
                Sync imports QuickBooks accounts into a new COA version. Manual uploads remain the
                highest-precedence source and are not overwritten.
              </p>
              <div className="quartz-button-stack">
                <button
                  className="primary-button"
                  disabled={isPending || connectionStatus?.status !== "connected"}
                  onClick={handleSyncCoa}
                  type="button"
                >
                  {isPending ? "Syncing accounts..." : "Sync Chart of Accounts"}
                </button>
              </div>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Operational Notes</p>
                  <h2 className="quartz-section-title">Integration Guardrails</h2>
                </div>
              </div>
              <div className="quartz-settings-info-stack">
                <div className="quartz-compact-pill">Manual COA uploads always win precedence</div>
                <div className="quartz-compact-pill">Posting packages remain governed</div>
                <div className="quartz-compact-pill">Reconnect is explicit after token loss</div>
              </div>
            </article>

            <article className="quartz-card quartz-settings-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <div>
                  <p className="quartz-card-eyebrow">Navigation</p>
                  <h2 className="quartz-section-title">Related Workspaces</h2>
                </div>
              </div>
              <div className="quartz-button-stack">
                <Link className="secondary-button" href={`/entities/${entityId}`}>
                  Back to Entity Home
                </Link>
                <Link className="secondary-button" href={`/entities/${entityId}/coa`}>
                  Open Chart of Accounts
                </Link>
              </div>
            </article>
          </div>
        </section>
      </section>
    </div>
  );
}

/**
 * Purpose: Load QuickBooks status for the current entity route.
 * Inputs: Entity UUID and state update callbacks.
 * Outputs: None; callers receive loaded status through callbacks.
 * Behavior: Uses typed error messages without leaking credential details.
 */
async function loadQuickBooksStatus(options: {
  entityId: string;
  onError: (message: string | null) => void;
  onLoaded: (status: QuickBooksConnectionStatusResponse) => void;
  onLoadingChange: (isLoading: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  try {
    const status = await readQuickBooksStatus(options.entityId);
    options.onLoaded(status);
    options.onError(null);
  } catch (error: unknown) {
    options.onError(resolveQuickBooksErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveQuickBooksErrorMessage(error: unknown): string {
  if (error instanceof QuickBooksApiError) {
    return error.message;
  }
  return "The QuickBooks request failed. Reload and try again.";
}

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatStatus(value: string): string {
  return value.replaceAll("_", " ");
}
