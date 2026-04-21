/*
Purpose: Render the operational entity home with live close-run context and start-close controls.
Scope: Client-side entity and close-run reads, plus governed close-run creation through the same-origin API.
Dependencies: React hooks, shared workflow metadata, Next.js routing, and entity/close-run helpers.
*/

"use client";

import { getWorkflowPhaseDefinition, type WorkflowPhase } from "@accounting-ai-agent/ui";
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
import { QuartzIcon, type QuartzIconName } from "../../../../components/layout/QuartzIcons";
import {
  CloseRunApiError,
  createCloseRun,
  deriveCloseRunAttention,
  findActivePhase,
  formatCloseRunDateTime,
  formatCloseRunPeriod,
  getCloseRunPhaseStatusLabel,
  getCloseRunStatusLabel,
  listCloseRuns,
  type CloseRunSummary,
} from "../../../../lib/close-runs";
import {
  EntityApiError,
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
  reportingCurrency: string;
};

type MetricTile = {
  label: string;
  meta: string;
  tone?: "error" | "success" | undefined;
  value: string;
};

type TaskItem = {
  detail: string;
  href: string;
  icon: QuartzIconName;
  label: string;
  title: string;
  tone: "error" | "neutral" | "success" | "warning";
};

type PhaseProgressRow = {
  color: string;
  label: string;
  percent: number;
  statusLabel: string;
};

const defaultCreateCloseRunFormState: CreateCloseRunFormState = {
  periodEnd: "",
  periodStart: "",
  reportingCurrency: "NGN",
};

export default function EntityWorkspacePage({
  params,
}: Readonly<EntityWorkspacePageProps>): ReactElement {
  const { entityId } = use(params);
  const router = useRouter();
  const [entity, setEntity] = useState<EntityWorkspace | null>(null);
  const [entityErrorMessage, setEntityErrorMessage] = useState<string | null>(null);
  const [closeRunErrorMessage, setCloseRunErrorMessage] = useState<string | null>(null);
  const [closeRuns, setCloseRuns] = useState<readonly CloseRunSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isPending, startTransition] = useTransition();
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
  const attention = useMemo(
    () => (activeCloseRun ? deriveCloseRunAttention(activeCloseRun) : null),
    [activeCloseRun],
  );
  const reportingCurrency =
    activeCloseRun?.reportingCurrency ??
    entity?.base_currency ??
    closeRunFormState.reportingCurrency;
  const metricTiles = useMemo(
    () => buildEntityMetricTiles(entity, closeRuns, activeCloseRun),
    [activeCloseRun, closeRuns, entity],
  );
  const taskItems = useMemo(
    () => buildEntityTaskItems(activeCloseRun, entityId),
    [activeCloseRun, entityId],
  );
  const phaseItems = useMemo(
    () => (activeCloseRun ? buildPhaseProgressRows(activeCloseRun) : []),
    [activeCloseRun],
  );
  const recentActivityEvents = useMemo(
    () => (entity ? entity.activity_events.slice(0, 3) : []),
    [entity],
  );
  const primaryCloseHref = activeCloseRun
    ? `/entities/${entityId}/close-runs/${activeCloseRun.id}`
    : null;
  const primaryWorkspaceHref = activeCloseRun
    ? resolvePhaseWorkspaceHref(
        entityId,
        activeCloseRun.id,
        findActivePhase(activeCloseRun)?.phase ?? "collection",
      )
    : null;

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
        reporting_currency: emptyStringToNull(closeRunFormState.reportingCurrency),
      })
        .then((createdCloseRun) => {
          setCloseRuns((currentCloseRuns) => [createdCloseRun, ...currentCloseRuns]);
          setCloseRunFormState({
            ...defaultCreateCloseRunFormState,
            reportingCurrency: entity?.base_currency ?? "NGN",
          });
          router.push(`/entities/${entityId}/close-runs/${createdCloseRun.id}`);
          router.refresh();
        })
        .catch((error: unknown) => {
          setCloseRunErrorMessage(resolveWorkspaceViewErrorMessage(error));
        });
    });
  };

  if (isLoading) {
    return (
      <div className="quartz-page quartz-workspace-layout">
        <section className="quartz-main-panel">
          <div className="quartz-empty-state">Loading entity home...</div>
        </section>
        <aside className="quartz-right-rail" />
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
        <aside className="quartz-right-rail" />
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
            {primaryCloseHref ? (
              <Link className="secondary-button" href={primaryCloseHref}>
                Open Active Close
              </Link>
            ) : (
              <a className="secondary-button" href="#new-close-run">
                Start Close Run
              </a>
            )}
            <div className="quartz-status-badge neutral">
              Reporting Currency: {reportingCurrency}
            </div>
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
          <div className="quartz-split-grid quartz-split-grid-halves">
            <article className="quartz-table-shell">
              <div className="quartz-task-row quartz-surface-row">
                <h2 className="quartz-kpi-label quartz-kpi-heading">Priority Queue</h2>
                <span className="quartz-pill-count">{taskItems.length}</span>
              </div>
              <div className="quartz-task-list">
                {taskItems.length === 0 ? (
                  <div className="quartz-task-row">
                    <span className="form-helper">
                      Create the first close run to begin task routing.
                    </span>
                  </div>
                ) : (
                  taskItems.map((item) => (
                    <div className="quartz-task-row" key={item.title}>
                      <div className="quartz-task-title">
                        <QuartzIcon className="quartz-inline-icon" name={item.icon} />
                        <div>
                          <strong>{item.title}</strong>
                          <div className="quartz-table-secondary">{item.detail}</div>
                        </div>
                      </div>
                      <Link className={`quartz-status-badge ${item.tone}`} href={item.href}>
                        {item.label}
                      </Link>
                    </div>
                  ))
                )}
              </div>
            </article>

            <article className="quartz-card">
              <div className="quartz-section-header quartz-section-header-tight">
                <h2 className="quartz-kpi-label quartz-kpi-heading">Close Progress</h2>
                {activeCloseRun ? (
                  <Link
                    className="quartz-filter-link"
                    href={primaryWorkspaceHref ?? primaryCloseHref ?? "#new-close-run"}
                  >
                    <QuartzIcon className="quartz-inline-icon" name="close" />
                    Continue
                  </Link>
                ) : null}
              </div>
              <div className="quartz-mini-list">
                {phaseItems.length === 0 ? (
                  <p className="form-helper">
                    Phase progress will appear here once a close run is active for this entity.
                  </p>
                ) : (
                  phaseItems.map((item) => (
                    <div className="quartz-mini-item" key={item.label}>
                      <div className="quartz-form-row">
                        <span className="quartz-table-secondary">{item.label}</span>
                        <span className="quartz-table-secondary">{item.statusLabel}</span>
                      </div>
                      <div className="quartz-progress-track">
                        <div
                          className="quartz-progress-bar"
                          style={{
                            background: item.color,
                            width: `${item.percent}%`,
                          }}
                        />
                      </div>
                    </div>
                  ))
                )}
              </div>
            </article>
          </div>
        </section>

        <section className="quartz-section">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Close Run Ledger</h2>
            <Link className="quartz-filter-link" href="/entities">
              <QuartzIcon className="quartz-inline-icon" name="filter" />
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
                            {activePhase
                              ? getWorkflowPhaseDefinition(activePhase.phase).label
                              : "Complete"}
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

        <section className="quartz-section" id="new-close-run">
          <div className="quartz-section-header">
            <h2 className="quartz-section-title">Start Close Run</h2>
            <span className="quartz-filter-link">
              Define the period and begin the governed workflow.
            </span>
          </div>

          <div className="quartz-card">
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

              <div className="quartz-form-row">
                <label className="quartz-form-label quartz-grow">
                  <span>Reporting Currency</span>
                  <input
                    className="text-input"
                    maxLength={3}
                    onChange={handleCloseRunFieldChange("reportingCurrency")}
                    type="text"
                    value={closeRunFormState.reportingCurrency}
                  />
                </label>

                <button className="primary-button" disabled={isPending} type="submit">
                  {isPending ? "Creating close run..." : "Start Close Run"}
                </button>
              </div>
            </form>

            {closeRunErrorMessage ? (
              <div className="status-banner warning quartz-section" role="status">
                {closeRunErrorMessage}
              </div>
            ) : null}
          </div>
        </section>
      </section>

      <aside className="quartz-right-rail">
        <div className="quartz-right-rail-header">
          <QuartzIcon className="quartz-inline-icon" name="assistant" />
          <div>
            <h2 className="quartz-right-rail-title">Omni-Assistant</h2>
            <p className="quartz-right-rail-subtitle">Entity intelligence</p>
          </div>
        </div>

        <div className="quartz-right-rail-body">
          <article className="quartz-card ai">
            <p
              className={`quartz-card-eyebrow ${attention?.tone === "warning" ? "error" : "secondary"}`}
            >
              {attention?.tone === "warning" ? "Attention required" : "Current focus"}
            </p>
            <h3>{attention?.label ?? "Workspace ready"}</h3>
            <p className="form-helper">
              {attention?.detail ??
                "This workspace is configured and ready for its next governed period-close cycle."}
            </p>
            <div className="quartz-button-row">
              {primaryWorkspaceHref ? (
                <Link className="secondary-button" href={primaryWorkspaceHref}>
                  Continue Workflow
                </Link>
              ) : (
                <a className="secondary-button" href="#new-close-run">
                  Start Close
                </a>
              )}
            </div>
          </article>

          <article className="quartz-card">
            <p className="quartz-card-eyebrow">Workspace ownership</p>
            <h3>{entity.default_actor?.full_name ?? "Default actor not assigned"}</h3>
            <p className="form-helper">
              {entity.member_count} active members • {formatAutonomyMode(entity.autonomy_mode)}{" "}
              posture
            </p>
            <div className="quartz-mini-list">
              <div className="quartz-mini-item">
                <span className="quartz-table-secondary">Legal entity</span>
                <strong>{entity.legal_name ?? entity.name}</strong>
              </div>
              <div className="quartz-mini-item">
                <span className="quartz-table-secondary">Base ledger</span>
                <strong>
                  {entity.base_currency} • {entity.timezone}
                </strong>
              </div>
            </div>
          </article>

          <article className="quartz-card">
            <p className="quartz-card-eyebrow">Recent activity</p>
            <div className="quartz-mini-list">
              {recentActivityEvents.length === 0 ? (
                <p className="form-helper">
                  Uploads, approvals, and routing events will appear here.
                </p>
              ) : (
                recentActivityEvents.map((event) => (
                  <div className="quartz-mini-item" key={event.id}>
                    <strong>{event.summary}</strong>
                    <span className="quartz-mini-meta">
                      {formatCloseRunDateTime(event.created_at)} via {event.source_surface}
                    </span>
                  </div>
                ))
              )}
            </div>
          </article>
        </div>

        <div className="quartz-right-rail-footer">
          <Link
            className="primary-button"
            href={
              activeCloseRun
                ? `/entities/${entity.id}/close-runs/${activeCloseRun.id}/chat`
                : `/entities/${entity.id}`
            }
          >
            {activeCloseRun ? "Open Assistant" : "Stay in Workspace"}
          </Link>
        </div>
      </aside>
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
  try {
    const workspace = await readEntityWorkspace(options.entityId);
    options.onLoaded(workspace);
    options.onError(null);

    try {
      const closeRuns = await listCloseRuns(options.entityId);
      options.onCloseRunsLoaded(closeRuns);
      options.onCloseRunError(null);
    } catch (error: unknown) {
      options.onCloseRunsLoaded([]);
      options.onCloseRunError(resolveWorkspaceViewErrorMessage(error));
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

function emptyStringToNull(value: string): string | null {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : null;
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
  activeCloseRun: Readonly<CloseRunSummary> | null,
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
  const attention = activeCloseRun ? deriveCloseRunAttention(activeCloseRun) : null;
  const activePhase = activeCloseRun ? findActivePhase(activeCloseRun) : null;

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
    {
      label: "Current Focus",
      meta: activeCloseRun
        ? formatCloseRunPeriod(activeCloseRun)
        : "Start the next governed period",
      tone:
        attention?.tone === "warning"
          ? "error"
          : attention?.tone === "success"
            ? "success"
            : undefined,
      value: activePhase ? getWorkflowPhaseDefinition(activePhase.phase).label : "No active close",
    },
  ];
}

function buildEntityTaskItems(
  activeCloseRun: Readonly<CloseRunSummary> | null,
  entityId: string,
): readonly TaskItem[] {
  if (activeCloseRun === null) {
    return [];
  }

  const activePhase = findActivePhase(activeCloseRun);
  const attention = deriveCloseRunAttention(activeCloseRun);
  const currentPhase = activePhase?.phase ?? "collection";

  return [
    {
      detail: attention.detail,
      href: resolvePhaseWorkspaceHref(entityId, activeCloseRun.id, currentPhase),
      icon: resolveTaskIcon(currentPhase, attention.tone),
      label: resolvePrimaryTaskLabel(currentPhase),
      title: resolvePrimaryTaskTitle(currentPhase),
      tone: mapAttentionToneToBadge(attention.tone),
    },
    {
      detail: "Ask grounded questions, inspect evidence, or route into the next control point.",
      href: `/entities/${entityId}/close-runs/${activeCloseRun.id}/chat`,
      icon: "assistant",
      label: "Open",
      title: "Assistant briefing",
      tone: "neutral",
    },
    {
      detail: "Return to the full period control tower with lifecycle and release controls.",
      href: `/entities/${entityId}/close-runs/${activeCloseRun.id}`,
      icon: "close",
      label: "Open",
      title: "Close mission control",
      tone: "neutral",
    },
  ];
}

function buildPhaseProgressRows(closeRun: Readonly<CloseRunSummary>): readonly PhaseProgressRow[] {
  return closeRun.workflowState.phaseStates.map((phaseState) => ({
    color: resolvePhaseColor(phaseState.status),
    label: getWorkflowPhaseDefinition(phaseState.phase).label,
    percent: resolvePhasePercent(phaseState.status),
    statusLabel: getCloseRunPhaseStatusLabel(phaseState.status),
  }));
}

function resolvePhaseWorkspaceHref(
  entityId: string,
  closeRunId: string,
  phase: WorkflowPhase,
): string {
  switch (phase) {
    case "collection":
      return `/entities/${entityId}/close-runs/${closeRunId}/documents`;
    case "processing":
      return `/entities/${entityId}/close-runs/${closeRunId}/recommendations`;
    case "reconciliation":
      return `/entities/${entityId}/close-runs/${closeRunId}/reconciliation`;
    case "reporting":
      return `/entities/${entityId}/close-runs/${closeRunId}/reports`;
    case "review_signoff":
      return `/entities/${entityId}/close-runs/${closeRunId}/exports`;
  }
}

function resolvePrimaryTaskTitle(phase: WorkflowPhase): string {
  switch (phase) {
    case "collection":
      return "Clear inputs and source evidence";
    case "processing":
      return "Review journals and recommendations";
    case "reconciliation":
      return "Resolve reconciliation exceptions";
    case "reporting":
      return "Generate reports and commentary";
    case "review_signoff":
      return "Complete sign-off and release";
  }
}

function resolvePrimaryTaskLabel(phase: WorkflowPhase): string {
  switch (phase) {
    case "collection":
      return "Open Inputs";
    case "processing":
      return "Review Journals";
    case "reconciliation":
      return "Open Reconciliation";
    case "reporting":
      return "Open Reports";
    case "review_signoff":
      return "Open Sign-Off";
  }
}

function resolveTaskIcon(
  phase: WorkflowPhase,
  tone: ReturnType<typeof deriveCloseRunAttention>["tone"],
): QuartzIconName {
  if (tone === "warning") {
    return "warning";
  }

  switch (phase) {
    case "collection":
      return "close";
    case "processing":
      return "sparkle";
    case "reconciliation":
      return "check";
    case "reporting":
      return "portfolio";
    case "review_signoff":
      return "assistant";
  }
}

function mapAttentionToneToBadge(
  tone: ReturnType<typeof deriveCloseRunAttention>["tone"],
): TaskItem["tone"] {
  switch (tone) {
    case "warning":
      return "error";
    case "success":
      return "success";
    default:
      return "warning";
  }
}

function resolvePhaseColor(
  status: CloseRunSummary["workflowState"]["phaseStates"][number]["status"],
): string {
  switch (status) {
    case "completed":
      return "var(--quartz-success)";
    case "ready":
      return "var(--quartz-gold)";
    case "blocked":
      return "var(--quartz-error)";
    case "in_progress":
      return "var(--quartz-secondary)";
    case "not_started":
      return "var(--quartz-border)";
  }
}

function resolvePhasePercent(
  status: CloseRunSummary["workflowState"]["phaseStates"][number]["status"],
): number {
  switch (status) {
    case "completed":
      return 100;
    case "ready":
      return 84;
    case "blocked":
      return 62;
    case "in_progress":
      return 46;
    case "not_started":
      return 10;
  }
}

function formatAutonomyMode(value: string): string {
  return value
    .split("_")
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(" ");
}
