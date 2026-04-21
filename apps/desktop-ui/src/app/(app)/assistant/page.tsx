"use client";

import Link from "next/link";
import { useEffect, useMemo, useState, type ReactElement } from "react";
import { ChatRail } from "../../../components/chat/ChatRail";
import { QuartzIcon } from "../../../components/layout/QuartzIcons";
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
import { readRememberedCloseContext } from "../../../lib/workspace-navigation";

export default function GlobalAssistantPage(): ReactElement {
  const dashboardSnapshot = readDashboardBootstrapSnapshot();
  const entityListSnapshot = readEntityListSnapshot();

  const [dashboardEntries, setDashboardEntries] = useState<readonly DashboardEntityRuns[]>(
    () => dashboardSnapshot ?? [],
  );
  const [entities, setEntities] = useState<readonly EntitySummary[]>(
    () => entityListSnapshot?.entities ?? [],
  );
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(
    () => dashboardSnapshot === null && entityListSnapshot === null,
  );

  useEffect(() => {
    let isActive = true;

    async function loadAssistantContext(): Promise<void> {
      setIsLoading(dashboardSnapshot === null && entityListSnapshot === null);
      setErrorMessage(null);

      try {
        const [resolvedDashboard, resolvedEntities] = await Promise.all([
          dashboardSnapshot ? Promise.resolve(dashboardSnapshot) : readDashboardBootstrap(),
          entityListSnapshot ? Promise.resolve(entityListSnapshot) : listEntities(),
        ]);

        if (!isActive) {
          return;
        }

        setDashboardEntries(resolvedDashboard);
        setEntities(resolvedEntities.entities);
      } catch (error: unknown) {
        if (!isActive) {
          return;
        }
        setErrorMessage(
          error instanceof EntityApiError
            ? error.message
            : "The assistant could not load your workspace context.",
        );
      } finally {
        if (isActive) {
          setIsLoading(false);
        }
      }
    }

    void loadAssistantContext();

    return () => {
      isActive = false;
    };
  }, [dashboardSnapshot, entityListSnapshot]);

  const preferredEntityId = useMemo(
    () =>
      resolvePreferredEntityId({
        dashboardEntries,
        entities,
        rememberedEntityId: readRememberedCloseContext()?.entityId ?? null,
      }),
    [dashboardEntries, entities],
  );

  const selectedEntity =
    entities.find((entity) => entity.id === preferredEntityId) ??
    entities[0] ??
    null;

  return (
    <div
      className="quartz-page quartz-chat-page"
      style={{
        display: "grid",
        gap: 16,
        gridTemplateRows: selectedEntity ? "auto minmax(0, 1fr)" : "auto",
        padding: 0,
      }}
    >
      <section className="quartz-card" style={{ display: "grid", gap: 14, padding: 18 }}>
        <div
          style={{
            alignItems: "center",
            display: "flex",
            flexWrap: "wrap",
            gap: 12,
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "grid", gap: 6 }}>
            <div
              style={{
                alignItems: "center",
                display: "inline-flex",
                gap: 8,
              }}
            >
              <p className="quartz-card-eyebrow" style={{ margin: 0 }}>
                Assistant
              </p>
              <span className="quartz-status-badge neutral">Global</span>
            </div>
            <h1 style={{ margin: 0 }}>Global Assistant</h1>
            <p className="quartz-page-subtitle" style={{ margin: 0 }}>
              Work directly in chat across your accessible workspaces. If you want to change
              workspace context, just ask in the conversation.
            </p>
          </div>

          <div className="quartz-page-toolbar">
            <Link className="secondary-button quartz-toolbar-button" href="/entities/new">
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              New Workspace
            </Link>
          </div>
        </div>

        {errorMessage ? (
          <div className="status-banner warning" role="status">
            {errorMessage}
          </div>
        ) : null}

        {entities.length > 0 ? (
          <div style={{ display: "grid", gap: 4 }}>
            <span className="quartz-queue-meta">
              {entities.length} workspace{entities.length === 1 ? "" : "s"} available to this
              operator
            </span>
            <span className="quartz-table-secondary">
              Open Assistant from an entity or close run when you want a hard-pinned scoped chat.
            </span>
          </div>
        ) : null}
      </section>

      {isLoading && selectedEntity === null ? (
        <section className="quartz-card" style={{ padding: 24 }}>
          <div className="quartz-empty-state">Preparing the global assistant...</div>
        </section>
      ) : null}

      {!isLoading && entities.length === 0 ? (
        <section className="quartz-card" style={{ padding: 24 }}>
          <div className="quartz-empty-state" style={{ display: "grid", gap: 16 }}>
            <div>
              No workspaces exist yet. Create the first workspace before opening the assistant.
            </div>
            <div>
              <Link className="primary-button" href="/entities/new">
                Create Workspace
              </Link>
            </div>
          </div>
        </section>
      ) : null}

      {selectedEntity ? (
        <div className="quartz-chat-workbench-shell" key={selectedEntity.id}>
          <ChatRail assistantMode="global" entityId={selectedEntity.id} presentation="workspace" />
        </div>
      ) : null}
    </div>
  );
}

function resolvePreferredEntityId(options: {
  dashboardEntries: readonly DashboardEntityRuns[];
  entities: readonly EntitySummary[];
  rememberedEntityId: string | null;
}): string | null {
  const candidateIds = new Set(options.entities.map((entity) => entity.id));
  if (options.rememberedEntityId && candidateIds.has(options.rememberedEntityId)) {
    return options.rememberedEntityId;
  }

  for (const entry of options.dashboardEntries) {
    if (candidateIds.has(entry.entity.id)) {
      return entry.entity.id;
    }
  }

  return options.entities[0]?.id ?? null;
}
