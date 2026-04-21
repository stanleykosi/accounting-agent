/*
Purpose: Render the operational entity home with live close-run context and start-close controls.
Scope: Client-side entity and close-run reads, plus governed close-run creation through the same-origin API.
Dependencies: React hooks, shared workflow metadata, Next.js routing, and entity/close-run helpers.
*/

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  use,
  useEffect,
  useMemo,
  useState,
  useTransition,
  type ChangeEvent,
  type FormEvent,
  type ReactElement,
} from "react";
import { QuartzIcon } from "../../../../components/layout/QuartzIcons";
import {
  CloseRunApiError,
  createCloseRun,
  deriveCloseRunAttention,
  findActivePhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunStatusLabel,
  listCloseRuns,
  readCloseRunListSnapshot,
  type CloseRunSummary,
} from "../../../../lib/close-runs";
import {
  EntityApiError,
  readEntityWorkspaceSnapshot,
  readEntityWorkspace,
  type EntityWorkspace,
} from "../../../../lib/entities/api";

type EntityWorkspacePageProps = {
  params: Promise<{
    entityId: string;
  }>;
};

type CreateCloseRunFormState = {
  periodEnd: string;
  periodStart: string;
};

type MetricTile = {
  label: string;
  meta: string;
  tone?: "error" | "success" | undefined;
  value: string;
};

const defaultCreateCloseRunFormState: CreateCloseRunFormState = {
  periodEnd: "",
  periodStart: "",
};

export default function EntityWorkspacePage({
  params,
}: Readonly<EntityWorkspacePageProps>): ReactElement {
  const { entityId } = use(params);
  const entitySnapshot = readEntityWorkspaceSnapshot(entityId);
  const closeRunSnapshot = readCloseRunListSnapshot(entityId);
  const router = useRouter();
  const [entity, setEntity] = useState<EntityWorkspace | null>(entitySnapshot);
  const [entityErrorMessage, setEntityErrorMessage] = useState<string | null>(null);
  const [closeRunErrorMessage, setCloseRunErrorMessage] = useState<string | null>(null);
  const [closeRuns, setCloseRuns] = useState<readonly CloseRunSummary[]>(closeRunSnapshot ?? []);
  const [isLoading, setIsLoading] = useState(
    () => entitySnapshot === null || closeRunSnapshot === null,
  );
  const [isPending, startTransition] = useTransition();
  const [isCreateCloseRunDialogOpen, setIsCreateCloseRunDialogOpen] = useState(false);
  const [closeRunFormState, setCloseRunFormState] = useState<CreateCloseRunFormState>(
    defaultCreateCloseRunFormState,
  );

  useEffect(() => {
    void loadWorkspaceView({
      entityId,
      onCloseRunsLoaded: setCloseRuns,
      onCloseRunError: setCloseRunErrorMessage,
      onError: setEntityErrorMessage,
      onLoaded: setEntity,
      onLoadingChange: setIsLoading,
    });
  }, [entityId]);

  const activeCloseRun = useMemo(() => findWorkingCloseRun(closeRuns), [closeRuns]);
  const metricTiles = useMemo(
    () => buildEntityMetricTiles(entity, closeRuns),
    [closeRuns, entity],
  );
  const handleCloseRunFieldChange =
    (fieldName: keyof CreateCloseRunFormState) =>
    (event: ChangeEvent<HTMLInputElement>): void => {
      setCloseRunFormState((currentState) => ({
        ...currentState,
        [fieldName]: event.target.value,
      }));
    };

  const handleCreateCloseRun = (event: FormEvent<HTMLFormElement>): void => {
    event.preventDefault();
    setCloseRunErrorMessage(null);

    startTransition(() => {
      void createCloseRun(entityId, {
        period_end: closeRunFormState.periodEnd,
        period_start: closeRunFormState.periodStart,
      })
        .then((createdCloseRun) => {
          setCloseRuns((currentCloseRuns) => [createdCloseRun, ...currentCloseRuns]);
          setCloseRunFormState(defaultCreateCloseRunFormState);
          setIsCreateCloseRunDialogOpen(false);
          router.push(`/entities/${entityId}/close-runs/${createdCloseRun.id}`);
          router.refresh();
        })
        .catch((error: unknown) => {
          setCloseRunErrorMessage(resolveWorkspaceViewErrorMessage(error));
        });
    });
  };

  useEffect(() => {
    if (!isCreateCloseRunDialogOpen) {
      return;
    }

    function handleEscape(event: KeyboardEvent): void {
      if (event.key === "Escape" && !isPending) {
        setIsCreateCloseRunDialogOpen(false);
      }
    }

    window.addEventListener("keydown", handleEscape);
    return () => {
      window.removeEventListener("keydown", handleEscape);
    };
  }, [isCreateCloseRunDialogOpen, isPending]);

  function openCreateCloseRunDialog(): void {
    setCloseRunErrorMessage(null);
    setCloseRunFormState(defaultCreateCloseRunFormState);
    setIsCreateCloseRunDialogOpen(true);
  }

  function closeCreateCloseRunDialog(): void {
    if (isPending) {
      return;
    }

    setIsCreateCloseRunDialogOpen(false);
    setCloseRunErrorMessage(null);
  }

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading entity home...</div>
        </section>
      </div>
    );
  }

  if (entity === null) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="status-banner danger" role="alert">
            {entityErrorMessage ?? "The entity workspace could not be loaded."}
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="quartz-page quartz-workspace-layout">
      <section className="quartz-main-panel">
        <header className="quartz-page-header">
          <div>
            <h1>{entity.name}</h1>
            <p className="quartz-page-subtitle">
              Entity Home • Operational Period:{" "}
              {activeCloseRun
                ? formatCloseRunPeriod(activeCloseRun)
                : "No active close in progress"}
            </p>
          </div>
          <div className="quartz-page-toolbar">
            <button className="primary-button" onClick={openCreateCloseRunDialog} type="button">
              Start Close Run
            </button>
          </div>
        </header>

        {entityErrorMessage ? (
          <div className="status-banner warning quartz-section" role="status">
            {entityErrorMessage}
          </div>
        ) : null}

        <section className="quartz-section">
          <div className="quartz-kpi-grid">
            {metricTiles.map((tile, index) => (
              <article
                className={
                  index === metricTiles.length - 1 ? "quartz-kpi-tile highlight" : "quartz-kpi-tile"
                }
                key={tile.label}
              >
                <p className="quartz-kpi-label">{tile.label}</p>
                <p className={`quartz-kpi-value ${tile.tone ?? ""}`.trim()}>{tile.value}</p>
                <p className="quartz-kpi-meta">{tile.meta}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Close Run Ledger</h2>
            <Link className="quartz-filter-link" href="/entities">
              <QuartzIcon className="quartz-inline-icon" name="entities" />
              Directory
            </Link>
          </div>

          <div className="quartz-table-shell">
            <table className="quartz-table">
              <thead>
                <tr>
                  <th>Period</th>
                  <th>Status</th>
                  <th>Current Gate</th>
                  <th>Last Updated</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {closeRuns.length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="quartz-empty-state">
                        No close runs exist yet for this workspace.
                      </div>
                    </td>
                  </tr>
                ) : (
                  closeRuns.map((closeRun) => {
                    const activePhase = findActivePhase(closeRun);
                    const rowAttention = deriveCloseRunAttention(closeRun);
                    const badgeTone =
                      rowAttention.tone === "warning"
                        ? "error"
                        : closeRun.status === "approved" || closeRun.status === "archived"
                          ? "success"
                          : "neutral";

                    return (
                      <tr
                        className={
                          rowAttention.tone === "warning" ? "quartz-table-row error" : undefined
                        }
                        key={closeRun.id}
                      >
                        <td>
                          <div className="quartz-table-primary">
                            {formatCloseRunPeriod(closeRun)}
                          </div>
                          <div className="quartz-table-secondary">
                            v{closeRun.currentVersionNo} • {closeRun.reportingCurrency}
                          </div>
                        </td>
                        <td>
                          <span className={`quartz-status-badge ${badgeTone}`}>
                            {getCloseRunStatusLabel(closeRun.status)}
                          </span>
                        </td>
                        <td>
                          <div className="quartz-table-primary">
                            {activePhase?.phase ? formatWorkflowPhaseLabel(activePhase.phase) : "Complete"}
                          </div>
                          <div className="quartz-table-secondary">{rowAttention.detail}</div>
                        </td>
                        <td>{formatCloseRunDateTime(closeRun.updatedAt)}</td>
                        <td className="quartz-table-center">
                          <Link
                            className="quartz-action-link"
                            href={`/entities/${entity.id}/close-runs/${closeRun.id}`}
                          >
                            Open
                          </Link>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </section>

      {isCreateCloseRunDialogOpen ? (
        <div
          aria-modal="true"
          className="quartz-modal-backdrop"
          onClick={closeCreateCloseRunDialog}
          role="dialog"
        >
          <div
            className="quartz-modal-card"
            onClick={(event) => event.stopPropagation()}
            role="document"
          >
            <div className="quartz-section-header quartz-section-header-tight">
              <div>
                <h2 className="quartz-section-title">Start Close Run</h2>
                <p className="quartz-page-subtitle">
                  Select the reporting period for {entity.name}.
                </p>
              </div>
              <button
                aria-label="Close"
                className="quartz-icon-button"
                onClick={closeCreateCloseRunDialog}
                type="button"
              >
                <QuartzIcon name="close" />
              </button>
            </div>

            <form className="quartz-setup-form" onSubmit={handleCreateCloseRun}>
              <div className="quartz-form-grid">
                <label className="quartz-form-label">
                  <span>Period Start</span>
                  <input
                    className="text-input"
                    onChange={handleCloseRunFieldChange("periodStart")}
                    required
                    type="date"
                    value={closeRunFormState.periodStart}
                  />
                </label>

                <label className="quartz-form-label">
                  <span>Period End</span>
                  <input
                    className="text-input"
                    onChange={handleCloseRunFieldChange("periodEnd")}
                    required
                    type="date"
                    value={closeRunFormState.periodEnd}
                  />
                </label>
              </div>

              {closeRunErrorMessage ? (
                <div className="status-banner warning" role="status">
                  {closeRunErrorMessage}
                </div>
              ) : null}

              <div className="quartz-form-row quartz-modal-actions">
                <button
                  className="secondary-button"
                  onClick={closeCreateCloseRunDialog}
                  type="button"
                >
                  Cancel
                </button>
                <button className="primary-button" disabled={isPending} type="submit">
                  {isPending ? "Creating close run..." : "Start Close Run"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}

async function loadWorkspaceView(options: {
  entityId: string;
  onCloseRunError: (message: string | null) => void;
  onCloseRunsLoaded: (closeRuns: readonly CloseRunSummary[]) => void;
  onError: (message: string | null) => void;
  onLoaded: (workspace: EntityWorkspace) => void;
  onLoadingChange: (value: boolean) => void;
}): Promise<void> {
  options.onLoadingChange(true);
  const [workspaceResult, closeRunsResult] = await Promise.allSettled([
    readEntityWorkspace(options.entityId),
    listCloseRuns(options.entityId),
  ]);

  try {
    if (workspaceResult.status === "rejected") {
      throw workspaceResult.reason;
    }

    options.onLoaded(workspaceResult.value);
    options.onError(null);

    if (closeRunsResult.status === "fulfilled") {
      options.onCloseRunsLoaded(closeRunsResult.value);
      options.onCloseRunError(null);
    } else {
      options.onCloseRunsLoaded([]);
      options.onCloseRunError(resolveWorkspaceViewErrorMessage(closeRunsResult.reason));
    }
  } catch (error: unknown) {
    options.onCloseRunsLoaded([]);
    options.onCloseRunError(null);
    options.onError(resolveWorkspaceViewErrorMessage(error));
  } finally {
    options.onLoadingChange(false);
  }
}

function resolveWorkspaceViewErrorMessage(error: unknown): string {
  if (error instanceof EntityApiError || error instanceof CloseRunApiError) {
    return error.message;
  }

  return "The entity workspace request failed. Reload the page and try again.";
}

function findWorkingCloseRun(closeRuns: readonly CloseRunSummary[]): CloseRunSummary | null {
  return (
    closeRuns.find((closeRun) => ["draft", "in_review", "reopened"].includes(closeRun.status)) ??
    closeRuns[0] ??
    null
  );
}

function buildEntityMetricTiles(
  entity: Readonly<EntityWorkspace> | null,
  closeRuns: readonly CloseRunSummary[],
): readonly MetricTile[] {
  const openCloseRuns = closeRuns.filter((closeRun) =>
    ["draft", "in_review", "reopened"].includes(closeRun.status),
  ).length;
  const blockedControls = closeRuns.reduce(
    (count, closeRun) =>
      count +
      closeRun.workflowState.phaseStates.filter((phaseState) => phaseState.status === "blocked")
        .length,
    0,
  );
  return [
    {
      label: "Open Periods",
      meta: `${closeRuns.length} close runs on record`,
      value: String(openCloseRuns),
    },
    {
      label: "Blocked Controls",
      meta:
        blockedControls > 0
          ? "Escalate blocked review gates before sign-off"
          : "No blocked workflow gates across active periods",
      tone: blockedControls > 0 ? "error" : "success",
      value: String(blockedControls),
    },
    {
      label: "Workspace Members",
      meta: entity?.default_actor?.full_name ?? "Default actor not assigned",
      value: String(entity?.member_count ?? 0),
    },
  ];
}

function formatWorkflowPhaseLabel(phase: CloseRunSummary["workflowState"]["phaseStates"][number]["phase"]): string {
  switch (phase) {
    case "collection":
      return "Inputs";
    case "processing":
      return "Journals";
    case "reconciliation":
      return "Reconciliation";
    case "reporting":
      return "Reports";
    case "review_signoff":
      return "Sign-Off";
  }
}
