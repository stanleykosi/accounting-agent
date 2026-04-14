/*
Purpose: Render the first-run local-service checklist used by the desktop setup experience.
Scope: Polling the setup health endpoint, surfacing recovery commands, and enabling operators to continue once the runtime is ready.
Dependencies: The setup health API route, shared setup contracts, and the global desktop UI styles.
*/

"use client";

import Link from "next/link";
import { useEffect, useState, useTransition, type ReactElement } from "react";
import type { DesktopSetupHealthSnapshot } from "../../lib/setup/types";

type HealthChecklistProps = {
  initialSnapshot: DesktopSetupHealthSnapshot;
  nextPath: string;
};

/**
 * Purpose: Show the canonical service-readiness checklist before the desktop UI enters protected workspace routes.
 * Inputs: The initial server-rendered health snapshot and the protected route to continue to after recovery.
 * Outputs: A client-rendered checklist with live polling, recovery guidance, and a continue action.
 * Behavior: Polls the same-origin setup-health route on an interval so operators can bring services up without reloading the page manually.
 */
export function HealthChecklist({
  initialSnapshot,
  nextPath,
}: Readonly<HealthChecklistProps>): ReactElement {
  const isHostedMode = initialSnapshot.mode === "hosted";
  const [snapshot, setSnapshot] = useState(initialSnapshot);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [isRefreshing, startTransition] = useTransition();

  const refreshSnapshot = async (): Promise<void> => {
    try {
      const response = await fetch("/api/setup/health", {
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(`The setup health endpoint returned HTTP ${response.status}.`);
      }

      const payload = (await response.json()) as DesktopSetupHealthSnapshot;
      setSnapshot(payload);
      setRequestError(null);
    } catch (error: unknown) {
      setRequestError(resolveRefreshError(error));
    }
  };

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      void refreshSnapshot();
    }, 10_000);

    return () => {
      window.clearInterval(intervalId);
    };
  }, []);

  const checkedAtLabel = formatCheckedAt(snapshot.checkedAt);

  return (
    <section className="setup-checklist">
      <div className="setup-checklist-header">
        <div>
          <p className="eyebrow">{isHostedMode ? "Hosted runtime" : "First-run validation"}</p>
          <h2>
            {isHostedMode
              ? "Hosted frontend routing is active."
              : "Confirm the local stack before entering the accounting workspace."}
          </h2>
        </div>

        <div className="setup-checklist-actions">
          {!isHostedMode ? (
            <button
              className="secondary-button"
              disabled={isRefreshing}
              onClick={() => {
                startTransition(() => {
                  void refreshSnapshot();
                });
              }}
              type="button"
            >
              {isRefreshing ? "Refreshing..." : "Refresh checks"}
            </button>
          ) : null}
          <Link
            className={snapshot.ready ? "primary-button" : "secondary-button disabled-link"}
            href={nextPath}
            prefetch={false}
          >
            {snapshot.ready ? "Continue to workspace" : "Waiting for services"}
          </Link>
        </div>
      </div>

      <p className="form-helper">
        {isHostedMode
          ? `Last checked ${checkedAtLabel}. Hosted mode skips loopback dependency checks and routes browser or remote desktop traffic through the deployed frontend origin.`
          : `Last checked ${checkedAtLabel}. The desktop shell blocks the main workflow routes until the local API, storage, and job infrastructure are reachable on loopback.`}
      </p>

      <div className={snapshot.ready ? "status-banner success" : "status-banner warning"}>
        {isHostedMode
          ? "Hosted runtime mode is active. Continue into the protected workspace."
          : snapshot.ready
            ? "All required local services are reachable. Continue into the protected workspace."
            : "One or more required services are still unavailable. Start the local demo stack, then refresh these checks."}
      </div>

      {requestError ? (
        <div className="status-banner danger" role="alert">
          {requestError}
        </div>
      ) : null}

      {snapshot.services.length > 0 ? (
        <div className="setup-service-grid">
          {snapshot.services.map((service) => (
            <article className="setup-service-card" key={service.id}>
              <div className="setup-service-header">
                <div>
                  <h3>{service.label}</h3>
                  {service.endpoint ? <p>{service.endpoint}</p> : null}
                </div>
                <span className={`setup-service-badge ${service.status}`}>
                  {service.status === "healthy" ? "Healthy" : "Blocked"}
                </span>
              </div>
              <p className="form-helper">{service.detail}</p>
              <p className="setup-service-latency">
                {service.latencyMs === null ? "No response yet" : `${service.latencyMs} ms`}
              </p>
            </article>
          ))}
        </div>
      ) : null}

      {snapshot.recoveryCommands.length > 0 ? (
        <div className="setup-command-panel">
          <h3>Canonical recovery commands</h3>
          <div className="setup-command-list" role="list">
            {snapshot.recoveryCommands.map((command) => (
              <code key={command} role="listitem">
                {command}
              </code>
            ))}
          </div>
        </div>
      ) : null}
    </section>
  );
}

function formatCheckedAt(value: string): string {
  const parsedDate = new Date(value);
  if (Number.isNaN(parsedDate.valueOf())) {
    return "just now";
  }

  return new Intl.DateTimeFormat("en-NG", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsedDate);
}

function resolveRefreshError(error: unknown): string {
  if (error instanceof Error && error.message.trim().length > 0) {
    return error.message;
  }

  return "The desktop setup checks could not be refreshed. Verify the Next.js sidecar is still running and retry.";
}
