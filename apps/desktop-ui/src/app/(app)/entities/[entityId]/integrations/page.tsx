/*
Purpose: Render entity integration management for QuickBooks Online.
Scope: Connection status, OAuth connect navigation, token revocation, and chart-of-accounts sync.
Dependencies: React client state, shared SurfaceCard, Next route params, and QuickBooks API helpers.
*/

"use client";

import { SurfaceCard } from "@accounting-ai-agent/ui";
import Link from "next/link";
import { use, useEffect, useState, useTransition, type ReactElement } from "react";
import {
  QuickBooksApiError,
  disconnectQuickBooks,
  readQuickBooksStatus,
  startQuickBooksConnection,
  syncQuickBooksCoa,
  type QuickBooksCoaSyncResponse,
  type QuickBooksConnectionStatusResponse,
} from "../../../../../lib/quickbooks";

type IntegrationsPageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

/**
 * Purpose: Render the QuickBooks-first integration workspace for one entity.
 * Inputs: Route params containing the target entity UUID.
 * Outputs: A client-rendered integration management page with sync and reconnect controls.
 * Behavior: Keeps OAuth, token refresh, and COA materialization in backend services.
 */
export default function EntityIntegrationsPage({
  params,
}: Readonly<IntegrationsPageProps>): ReactElement {
  const { entityId } = use(params);
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
      <div className="app-shell integrations-page">
        <SurfaceCard title="Loading Integrations" subtitle="QuickBooks Online">
          <p className="form-helper">Loading connection state and sync metadata...</p>
        </SurfaceCard>
      </div>
    );
  }

  return (
    <div className="app-shell integrations-page">
      <section className="hero-grid integrations-hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">Integrations</p>
          <h1>QuickBooks Online syncs accounts and informs external posting packages.</h1>
          <p className="lede">
            Connect a company realm, refresh credentials safely, and import chart-of-accounts
            accounts into the same versioned COA workspace used by manual uploads. Approved
            journals can then generate ERP posting packages with QuickBooks-aware context when a
            connection is available.
          </p>
          <div className="coa-hero-actions">
            <Link className="secondary-button" href={`/entities/${entityId}`}>
              Back to entity workspace
            </Link>
            <Link className="secondary-button" href={`/entities/${entityId}/coa`}>
              Open chart of accounts
            </Link>
          </div>
        </div>

        <SurfaceCard title="QuickBooks Online" subtitle="Connection state" tone="accent">
          <dl className="entity-meta-grid coa-summary-grid">
            <div>
              <dt>Status</dt>
              <dd>{formatStatus(connectionStatus?.status ?? "disconnected")}</dd>
            </div>
            <div>
              <dt>Realm</dt>
              <dd>{connectionStatus?.external_realm_id ?? "Not connected"}</dd>
            </div>
            <div>
              <dt>Last sync</dt>
              <dd>
                {connectionStatus?.last_sync_at
                  ? formatDateTime(connectionStatus.last_sync_at)
                  : "No sync yet"}
              </dd>
            </div>
            <div>
              <dt>Posting</dt>
              <dd>Not enabled</dd>
            </div>
          </dl>
        </SurfaceCard>
      </section>

      {errorMessage ? (
        <div className="status-banner danger" role="alert">
          {errorMessage}
        </div>
      ) : null}

      {connectionStatus?.recovery_action ? (
        <div className="status-banner warning" role="status">
          {connectionStatus.recovery_action}
        </div>
      ) : null}

      {syncResult ? (
        <div className="status-banner" role="status">
          {syncResult.message} COA version {syncResult.version_no}
          {syncResult.activated ? " is now active." : " was saved for activation when needed."}
        </div>
      ) : null}

      <section className="coa-grid">
        <SurfaceCard title="Connection" subtitle="OAuth and token lifecycle">
          <div className="integration-action-stack">
            <p className="form-helper">
              QuickBooks credentials are encrypted locally. Expired or revoked tokens require an
              explicit reconnect before another sync can run.
            </p>
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
        </SurfaceCard>

        <SurfaceCard title="Chart of Accounts Sync" subtitle="QuickBooks to active COA sets">
          <div className="integration-action-stack">
            <p className="form-helper">
              Sync imports QuickBooks accounts into a new COA version. Manual uploads remain the
              highest-precedence source and are not overwritten by QuickBooks.
            </p>
            <button
              className="primary-button"
              disabled={isPending || connectionStatus?.status !== "connected"}
              onClick={handleSyncCoa}
              type="button"
            >
              {isPending ? "Syncing accounts..." : "Sync chart of accounts"}
            </button>
          </div>
        </SurfaceCard>
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
